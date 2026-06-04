from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tend.agents import Agent, AgentContext, register
from tend.config import LLMSettings, Paths, Settings
from tend.errors import (
    Anomaly,
    ContextOverflowError,
    PromptAnomalyError,
    ResponseParseError,
    SchemaValidationError,
)
from tend.llm import LLMClient
from tend.observability import ProgressReporter, setup_logging
from tend.workflow import Workflow


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _stub_settings(tmp_path: Path) -> Settings:
    return Settings(
        llm=LLMSettings(
            base_url="http://stub.invalid",
            api_key="stub",
            model="stub-model",
            max_retries=0,
            max_concurrency=1,
        ),
        paths=Paths(
            repo_root=tmp_path,
            bird_root=tmp_path / "bird",
            proposals=tmp_path / "proposals",
            agent_prompts=tmp_path / "proposals" / "agent_prompts",
            schemas=tmp_path / "proposals" / "schemas",
            runs=tmp_path / "runs",
            dataset_out=tmp_path / "release",
        ),
        mongo_uri="mongodb://stub.invalid",
        stub=True,
        quiet=True,
        run_id="solver-observability-test",
    )


def test_anomaly_stream_keeps_message_context_and_transcript_ref(tmp_path: Path) -> None:
    log = setup_logging(tmp_path / "run", console=False)
    bound = log.bind(stage="solver", db_id="financial", agent="solver", record_id=17)

    transcript_ref = bound.save_transcript(
        "solver",
        "call-1",
        {"messages": [{"role": "user", "content": "solve"}], "response": "{}"},
    )
    bound.anomaly(
        SchemaValidationError("missing solver output", context={"missing": ["MQL"]}),
        transcript_ref=transcript_ref,
    )
    log.close()

    anomalies = _read_jsonl(tmp_path / "run" / "anomalies.jsonl")
    assert len(anomalies) == 1

    anomaly = anomalies[0]
    assert anomaly["anomaly"] == Anomaly.SCHEMA_INVALID.value
    assert anomaly["message"] == "missing solver output"
    assert anomaly["context"] == {"missing": ["MQL"]}
    assert anomaly["transcript_ref"] == transcript_ref
    assert (tmp_path / "run" / transcript_ref).exists()
    assert transcript_ref == "llm/solver/call-1.diagnostics.json"
    assert (tmp_path / "run" / "llm/solver/call-1.diagnostics.json").exists()
    diagnostics = json.loads((tmp_path / "run" / transcript_ref).read_text(encoding="utf-8"))
    assert diagnostics["messages"] == [{"role": "user", "content": "solve"}]
    assert diagnostics["response"] == "{}"
    assert diagnostics["markdown_transcript_enabled"] is False
    assert not (tmp_path / "run" / "llm/solver/call-1.md").exists()
    assert anomaly["missing"] == ["MQL"]
    assert anomaly["stage"] == "solver"
    assert anomaly["agent"] == "solver"


def test_llm_markdown_transcripts_are_optional_debug_artifacts(tmp_path: Path) -> None:
    log = setup_logging(
        tmp_path / "run",
        console=False,
        write_llm_markdown_transcripts=True,
    )

    transcript_ref = log.save_transcript(
        "solver",
        "call-debug",
        {"messages": [{"role": "user", "content": "solve"}], "response": "{}"},
    )
    log.close()

    assert transcript_ref == "llm/solver/call-debug.diagnostics.json"
    assert (tmp_path / "run" / transcript_ref).exists()
    assert (tmp_path / "run" / "llm/solver/call-debug.diagnostics.json").exists()
    diagnostics = json.loads(
        (tmp_path / "run" / "llm/solver/call-debug.diagnostics.json").read_text(
            encoding="utf-8"
        )
    )
    transcript_md = (
        tmp_path / "run" / diagnostics["markdown_transcript_ref"]
    ).read_text(encoding="utf-8")
    assert "## Messages" in transcript_md
    assert "> solve" in transcript_md
    assert diagnostics["markdown_transcript_enabled"] is True
    assert diagnostics["transcript_ref"] == transcript_ref
    assert diagnostics["diagnostics_ref"] == transcript_ref
    assert diagnostics["markdown_transcript_ref"] == "llm/solver/call-debug.debug.md"


def test_save_transcript_prefers_bound_agent_session_ref(tmp_path: Path) -> None:
    log = setup_logging(
        tmp_path / "run",
        console=False,
        write_llm_markdown_transcripts=True,
    )
    bound = log.bind(
        stage="solver",
        db_id="financial",
        record_id=17,
        agent_session_ref="agent/solver/session.md",
    )

    transcript_ref = bound.save_transcript(
        "solver",
        "call-session",
        {"messages": [{"role": "user", "content": "solve"}], "response": "{}"},
    )
    log.close()

    diagnostics_ref = "agent/solver/diagnostics/solver/call-session.diagnostics.json"
    assert transcript_ref == "agent/solver/session.md"
    diagnostics = json.loads((tmp_path / "run" / diagnostics_ref).read_text(encoding="utf-8"))
    assert diagnostics["transcript_ref"] == "agent/solver/session.md"
    assert diagnostics["diagnostics_ref"] == diagnostics_ref
    assert diagnostics["agent_session_ref"] == "agent/solver/session.md"
    assert diagnostics["stage"] == "solver"
    assert diagnostics["db_id"] == "financial"
    assert diagnostics["record_id"] == 17
    assert diagnostics["markdown_transcript_enabled"] is True
    assert (
        diagnostics["markdown_transcript_ref"]
        == "agent/solver/diagnostics/solver/call-session.debug.md"
    )
    assert (tmp_path / "run" / diagnostics["markdown_transcript_ref"]).exists()


def test_llm_result_preserves_session_transcript_and_diagnostics_ref(
    tmp_path: Path,
) -> None:
    log = setup_logging(
        tmp_path / "run",
        console=False,
        write_llm_markdown_transcripts=True,
    )
    bound = log.bind(
        stage="solver",
        db_id="financial",
        record_id=17,
        agent_session_ref="agent/solver/session.md",
    )
    client = LLMClient(_stub_settings(tmp_path), log)

    async def run() -> tuple[str, str]:
        result = await client.complete(
            agent="solver",
            logger=bound,
            messages=[{"role": "user", "content": "solve"}],
            expect_json=False,
        )
        return result.transcript_ref, result.diagnostics_ref

    transcript_ref, diagnostics_ref = asyncio.run(run())
    log.close()

    assert transcript_ref == "agent/solver/session.md"
    assert diagnostics_ref.startswith("agent/solver/diagnostics/solver/")
    assert diagnostics_ref.endswith(".diagnostics.json")
    diagnostics = json.loads((tmp_path / "run" / diagnostics_ref).read_text(encoding="utf-8"))
    assert diagnostics["transcript_ref"] == "agent/solver/session.md"
    assert diagnostics["diagnostics_ref"] == diagnostics_ref
    assert diagnostics["agent_session_ref"] == "agent/solver/session.md"
    assert diagnostics["markdown_transcript_ref"].endswith(".md")


def test_llm_prompt_anomalies_are_written_with_transcripts(tmp_path: Path, monkeypatch) -> None:
    log = setup_logging(tmp_path / "run", console=False)
    client = LLMClient(_stub_settings(tmp_path), log)

    async def context_overflow_call(*_args):
        raise ContextOverflowError(
            "context length exceeded",
            context={"provider": "stub", "limit_tokens": 8},
        )

    async def run() -> None:
        with pytest.raises(PromptAnomalyError):
            await client.complete(
                agent="solver",
                messages=[{"role": "user", "content": "   "}],
            )

        monkeypatch.setattr(client, "_raw_call", context_overflow_call)
        with pytest.raises(ContextOverflowError):
            await client.complete(
                agent="solver",
                messages=[{"role": "user", "content": "x" * 128}],
            )

    asyncio.run(run())
    log.close()

    anomalies = _read_jsonl(tmp_path / "run" / "anomalies.jsonl")
    by_kind = {record["anomaly"]: record for record in anomalies}

    prompt_record = by_kind[Anomaly.PROMPT_MALFORMED.value]
    assert prompt_record["error_type"] == "PromptAnomalyError"
    assert prompt_record["message"] == "message content empty or non-string"
    assert prompt_record["context"]["agent"] == "solver"
    assert prompt_record["context"]["model"] == "stub-model"
    assert prompt_record["context"]["transcript_ref"] == prompt_record["transcript_ref"]
    assert prompt_record["context"]["diagnostics_ref"] == prompt_record["diagnostics_ref"]
    assert (tmp_path / "run" / prompt_record["transcript_ref"]).exists()
    assert (tmp_path / "run" / prompt_record["diagnostics_ref"]).exists()
    prompt_diagnostics = json.loads(
        (tmp_path / "run" / prompt_record["diagnostics_ref"]).read_text(encoding="utf-8")
    )
    assert prompt_record["transcript_ref"].endswith(".diagnostics.json")
    assert prompt_diagnostics["markdown_transcript_enabled"] is False
    assert "message content empty or non-string" in json.dumps(prompt_diagnostics)

    overflow_record = by_kind[Anomaly.CONTEXT_OVERFLOW.value]
    assert overflow_record["error_type"] == "ContextOverflowError"
    assert overflow_record["message"] == "context length exceeded"
    assert overflow_record["context"]["provider"] == "stub"
    assert overflow_record["context"]["transcript_ref"] == overflow_record["transcript_ref"]
    assert overflow_record["context"]["diagnostics_ref"] == overflow_record["diagnostics_ref"]
    assert (tmp_path / "run" / overflow_record["transcript_ref"]).exists()
    assert (tmp_path / "run" / overflow_record["diagnostics_ref"]).exists()
    overflow_diagnostics = json.loads(
        (tmp_path / "run" / overflow_record["diagnostics_ref"]).read_text(encoding="utf-8")
    )
    assert overflow_record["transcript_ref"].endswith(".diagnostics.json")
    assert "context length exceeded" in json.dumps(overflow_diagnostics)


def test_llm_prompt_role_anomalies_are_written_with_transcripts(tmp_path: Path) -> None:
    log = setup_logging(tmp_path / "run", console=False)
    client = LLMClient(_stub_settings(tmp_path), log)

    async def run() -> None:
        with pytest.raises(PromptAnomalyError):
            await client.complete(
                agent="solver",
                messages=[{"role": "bad-role", "content": "x"}],
            )

    asyncio.run(run())
    log.close()

    anomalies = _read_jsonl(tmp_path / "run" / "anomalies.jsonl")
    assert anomalies[0]["anomaly"] == Anomaly.PROMPT_MALFORMED.value
    assert anomalies[0]["message"] == "message role is not supported"
    assert anomalies[0]["context"]["role"] == "bad-role"
    assert anomalies[0]["transcript_ref"].endswith(".diagnostics.json")
    assert (tmp_path / "run" / anomalies[0]["diagnostics_ref"]).exists()


def test_failed_llm_markdown_expands_attempt_details(tmp_path: Path, monkeypatch) -> None:
    log = setup_logging(
        tmp_path / "run",
        console=False,
        write_llm_markdown_transcripts=True,
    )
    client = LLMClient(_stub_settings(tmp_path), log)
    schema = {
        "type": "object",
        "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
    }

    async def raw_call(*_args):
        return "not-json", "stop", {"total_tokens": 1}, None

    async def run() -> None:
        monkeypatch.setattr(client, "_raw_call", raw_call)
        with pytest.raises(ResponseParseError):
            await client.complete(
                agent="solver",
                messages=[{"role": "user", "content": "go"}],
                schema=schema,
                json_repair_retries=0,
            )

    asyncio.run(run())
    log.close()

    anomaly = _read_jsonl(tmp_path / "run" / "anomalies.jsonl")[0]
    diagnostics = json.loads((tmp_path / "run" / anomaly["diagnostics_ref"]).read_text(
        encoding="utf-8"
    ))
    transcript_md = (tmp_path / "run" / diagnostics["markdown_transcript_ref"]).read_text(
        encoding="utf-8"
    )
    assert anomaly["transcript_ref"] == anomaly["diagnostics_ref"]
    assert anomaly["transcript_ref"].endswith(".diagnostics.json")
    assert anomaly["anomaly"] == Anomaly.PARSE_ERROR.value
    assert diagnostics["markdown_transcript_ref"].endswith(".md")
    assert "## Attempt Details" in transcript_md
    assert "#### Response" in transcript_md
    assert "> not-json" in transcript_md


def test_llm_diagnostics_include_json_safe_provider_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    log = setup_logging(tmp_path / "run", console=False)
    client = LLMClient(_stub_settings(tmp_path), log)
    schema = {
        "type": "object",
        "required": ["foo"],
        "properties": {"foo": {"type": "string"}},
        "additionalProperties": False,
    }
    raw_response = {
        "id": "chatcmpl-test",
        "model": "provider-model",
        "provider_request_id": "req-123",
        "truncation": {"mode": "disabled"},
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": '{"foo":"ok"}',
                    "refusal": None,
                },
            }
        ],
    }

    async def raw_call(*_args):
        return (
            '{"foo":"ok"}',
            "stop",
            {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            raw_response,
        )

    async def run() -> str:
        monkeypatch.setattr(client, "_raw_call", raw_call)
        result = await client.complete(
            agent="solver",
            messages=[{"role": "user", "content": "go"}],
            schema=schema,
        )
        return result.diagnostics_ref

    diagnostics_ref = asyncio.run(run())
    log.close()

    diagnostics = json.loads((tmp_path / "run" / diagnostics_ref).read_text(encoding="utf-8"))
    provider_metadata = diagnostics["provider_metadata"]
    assert provider_metadata["id"] == "chatcmpl-test"
    assert provider_metadata["finish_reason"] == "stop"
    assert provider_metadata["truncation"] == {"mode": "disabled"}
    assert provider_metadata["choices"][0]["finish_reason"] == "stop"
    assert provider_metadata["choices"][0]["message"]["refusal"] is None
    assert diagnostics["attempts"][0]["raw_response"]["provider_request_id"] == "req-123"


def test_complete_diagnostics_preserve_request_configuration(tmp_path: Path, monkeypatch) -> None:
    log = setup_logging(tmp_path / "run", console=False)
    client = LLMClient(_stub_settings(tmp_path), log)
    schema = {
        "type": "object",
        "required": ["foo"],
        "properties": {"foo": {"type": "string"}},
        "additionalProperties": False,
    }

    async def raw_call(*_args):
        return '{"foo":"ok"}', "stop", {"total_tokens": 1}, None

    async def run() -> str:
        monkeypatch.setattr(client, "_raw_call", raw_call)
        result = await client.complete(
            agent="solver",
            messages=[{"role": "user", "content": "go"}],
            schema=schema,
            expect_json=True,
            temperature=0.2,
            max_tokens=123,
            json_repair_retries=0,
        )
        return result.diagnostics_ref

    diagnostics_ref = asyncio.run(run())
    log.close()

    diagnostics = json.loads((tmp_path / "run" / diagnostics_ref).read_text(encoding="utf-8"))
    assert diagnostics["schema"] == schema
    assert diagnostics["expect_json"] is True
    assert diagnostics["temperature"] == 0.2
    assert diagnostics["max_tokens"] == 123
    assert diagnostics["json_repair_retries"] == 0


def test_workflow_isolated_raw_exceptions_emit_internal_anomalies(tmp_path: Path) -> None:
    log = setup_logging(tmp_path / "run", console=False)
    settings = _stub_settings(tmp_path)
    client = LLMClient(settings, log)
    ctx = AgentContext(settings=settings, llm=client, log=log)
    wf = Workflow(ctx)

    async def ok() -> str:
        return "ok"

    async def parallel_boom() -> str:
        raise RuntimeError("parallel raw boom")

    async def stage_boom(_item: str) -> str:
        raise RuntimeError("pipeline raw boom")

    async def run() -> None:
        parallel_out = await wf.parallel([ok, parallel_boom], isolate=True)
        assert parallel_out == ["ok", None]
        pipeline_out = await wf.pipeline(["case-1"], stage_boom, isolate=True)
        assert pipeline_out == [None]

    asyncio.run(run())
    log.close()

    anomalies = _read_jsonl(tmp_path / "run" / "anomalies.jsonl")
    by_primitive = {record["primitive"]: record for record in anomalies}
    assert by_primitive["parallel"]["anomaly"] == Anomaly.INTERNAL.value
    assert by_primitive["parallel"]["index"] == 1
    assert "parallel_boom" in by_primitive["parallel"]["item_repr"]
    assert "RuntimeError: parallel raw boom" in by_primitive["parallel"]["traceback"]
    assert by_primitive["pipeline"]["anomaly"] == Anomaly.INTERNAL.value
    assert by_primitive["pipeline"]["index"] == 0
    assert by_primitive["pipeline"]["stage_index"] == 0
    assert by_primitive["pipeline"]["item_repr"] == "'case-1'"
    assert "stage_boom" in by_primitive["pipeline"]["stage_repr"]
    assert "RuntimeError: pipeline raw boom" in by_primitive["pipeline"]["traceback"]


def test_agent_progress_task_ids_include_collection_metadata(tmp_path: Path) -> None:
    @register
    class ObservabilityTaskIdentityProbe(Agent):
        id = "obs_task_identity_probe"
        phase = "SOLVE-1"
        title = "identity probe"

        async def run(self, ctx: AgentContext, inputs: dict) -> dict:
            await asyncio.sleep(0)
            return {"collection": inputs["collection"]}

    log = setup_logging(tmp_path / "run", console=False)
    progress = ProgressReporter("solver-run", log, enabled=False)
    settings = _stub_settings(tmp_path)
    client = LLMClient(settings, log)
    ctx = AgentContext(
        settings=settings,
        llm=client,
        log=log,
        progress=progress,
        db_id="financial",
        record_id=17,
        group="solve:financial:17",
    )
    wf = Workflow(ctx)

    async def run() -> None:
        out = await wf.map_agent(
            "obs_task_identity_probe",
            [
                (ctx, {"collection": "account"}),
                (ctx, {"collection": "loan"}),
            ],
            isolate=False,
        )
        assert [item["collection"] for item in out] == ["account", "loan"]

    asyncio.run(run())
    task_ids = sorted(progress._tasks)
    summary = progress.summary()
    log.close()

    assert len(task_ids) == 2
    assert any(task_id.endswith(":collection=account") for task_id in task_ids)
    assert any(task_id.endswith(":collection=loan") for task_id in task_ids)
    assert summary["tasks"]["started"] == 2
    assert summary["tasks"]["ok"] == 2


def test_progress_group_total_switches_to_task_units(tmp_path: Path) -> None:
    log = setup_logging(tmp_path / "run", console=False)
    progress = ProgressReporter("solver-run", log, enabled=False)
    progress.add_group("records", "records", phase="B", total=1)

    progress.start_task("task-1", "first agent", group="records")
    progress.finish_task("task-1")
    progress.start_task("task-2", "second agent", group="records")
    progress.finish_task("task-2")
    summary = progress.summary()
    log.close()

    assert progress._groups["records"].total is None
    assert summary["tasks"]["started"] == 2
    assert summary["tasks"]["ok"] == 2


def test_progress_summary_counts_anomalies_by_kind(tmp_path: Path) -> None:
    log = setup_logging(tmp_path / "run", console=False)
    progress = ProgressReporter("solver-run", log, enabled=False)

    progress.start_task("task-1", "solver case", group="financial")
    log.anomaly(kind=Anomaly.PROMPT_MALFORMED, message="bad prompt")
    log.anomaly(kind=Anomaly.PROMPT_MALFORMED, message="bad prompt again")
    log.anomaly(kind=Anomaly.CONTEXT_OVERFLOW, message="prompt too large")
    progress.finish_task("task-1", ok=False, anomaly=Anomaly.PROMPT_MALFORMED.value)

    summary = progress.summary()
    ticker_messages = [record["message"] for record in progress._anoms]
    log.close()

    assert summary["tasks"]["started"] == 1
    assert summary["tasks"]["fail"] == 1
    assert summary["anomaly_total"] == 3
    assert summary["anomalies_by_kind"] == {
        Anomaly.PROMPT_MALFORMED.value: 2,
        Anomaly.CONTEXT_OVERFLOW.value: 1,
    }
    assert ticker_messages == ["bad prompt", "bad prompt again", "prompt too large"]


def test_progress_persists_snapshots_and_surfaces_warning_events(tmp_path: Path) -> None:
    log = setup_logging(tmp_path / "run", console=False)
    progress = ProgressReporter("solver-run", log, enabled=False)

    progress.phase("B")
    progress.add_group("financial", "financial", phase="B", total=1)
    progress.start_task("task-1", "MS", group="financial")
    log.warning(
        "llm_repair_retry",
        db_id="financial",
        agent="ms",
        call_id="abc",
        reason="schema_invalid",
    )
    progress.retry_task("task-1", detail="repair")
    progress.finish_task("task-1")
    summary = progress.summary()
    log.close()

    snapshots = _read_jsonl(tmp_path / "run" / "progress.jsonl")
    warning_events = _read_jsonl(tmp_path / "run" / "events.jsonl")
    assert all(item["record_type"] == "progress_snapshot" for item in snapshots)
    assert all(item["source"] == "tend_root_progress" for item in snapshots)
    assert summary["alerts_by_event"] == {"llm_repair_retry": 1}
    assert any(item["reason"] == "event" for item in snapshots)
    assert snapshots[-1]["alerts_by_event"] == {"llm_repair_retry": 1}
    assert snapshots[-1]["recent_alerts"][-1]["event"] == "llm_repair_retry"
    assert any(event["event"] == "llm_repair_retry" for event in warning_events)
