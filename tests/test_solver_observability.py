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
    assert transcript_ref == "llm/solver/call-1.md"
    assert (tmp_path / "run" / "llm/solver/call-1.diagnostics.json").exists()
    transcript_md = (tmp_path / "run" / transcript_ref).read_text(encoding="utf-8")
    assert "## Messages" in transcript_md
    assert "> solve" in transcript_md
    assert "### Content" in transcript_md
    assert anomaly["missing"] == ["MQL"]
    assert anomaly["stage"] == "solver"
    assert anomaly["agent"] == "solver"


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
    prompt_md = (tmp_path / "run" / prompt_record["transcript_ref"]).read_text(
        encoding="utf-8"
    )
    assert prompt_record["transcript_ref"].endswith(".md")
    assert "message content empty or non-string" in prompt_md
    assert "llm/solver/" in prompt_md

    overflow_record = by_kind[Anomaly.CONTEXT_OVERFLOW.value]
    assert overflow_record["error_type"] == "ContextOverflowError"
    assert overflow_record["message"] == "context length exceeded"
    assert overflow_record["context"]["provider"] == "stub"
    assert overflow_record["context"]["transcript_ref"] == overflow_record["transcript_ref"]
    assert overflow_record["context"]["diagnostics_ref"] == overflow_record["diagnostics_ref"]
    assert (tmp_path / "run" / overflow_record["transcript_ref"]).exists()
    assert (tmp_path / "run" / overflow_record["diagnostics_ref"]).exists()
    overflow_md = (tmp_path / "run" / overflow_record["transcript_ref"]).read_text(
        encoding="utf-8"
    )
    assert overflow_record["transcript_ref"].endswith(".md")
    assert "context length exceeded" in overflow_md


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


def test_workflow_isolated_raw_exceptions_emit_internal_anomalies(tmp_path: Path) -> None:
    log = setup_logging(tmp_path / "run", console=False)
    settings = _stub_settings(tmp_path)
    client = LLMClient(settings, log)
    ctx = AgentContext(settings=settings, llm=client, log=log)
    wf = Workflow(ctx, max_concurrency=2)

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
    wf = Workflow(ctx, max_concurrency=2)

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
