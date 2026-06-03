from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from tend.config import LLMSettings, Paths, Settings
from tend.llm.types import ToolCall, ToolLLMResult
from tend.observability import setup_logging
from tend.solver.eg import SmartEGPolicy, smart_solve_nlq_db_eg
from tend.solver.eg.contracts import SmartEGFailure, SmartEGPrediction
from tend.solver.eg.runtime import SYSTEM_PROMPT
from tend.solver.eg.tools import tool_schemas


class _Progress:
    def phase(self, _name): ...
    def event(self, **_fields): ...


class _Mongo:
    def list_collections(self, _db_id):
        return ["account"]

    def sample_documents(self, _db_id, collection, limit=3, **_kwargs):
        assert collection == "account"
        return [{"_id": 1, "loan": {"amount": 10}}, {"_id": 2}][:limit]

    def aggregate_readonly_bounded(self, _db_id, _mql, limit=50):
        return {"collection": "account", "count": min(2, limit), "sample": [{"_id": 1}]}


class _LLM:
    def __init__(self, calls: list[ToolCall | None]) -> None:
        self.calls = list(calls)
        self.requests: list[dict] = []

    async def complete_with_tools(self, **kwargs):
        self.requests.append(kwargs)
        call = self.calls.pop(0)
        if call is None:
            return ToolLLMResult(
                agent=kwargs["agent"],
                call_id=f"call_{len(self.requests)}",
                model="stub",
                assistant_message={"role": "assistant", "content": "natural language"},
                tool_calls=[],
                finish_reason="stop",
                usage={},
                cost={"source": "unavailable"},
                latency_s=0.01,
                attempts=1,
                transcript_ref="llm/smart_eg/call.md",
                diagnostics_ref="llm/smart_eg/call.diagnostics.json",
                provider_metadata={},
            )
        return ToolLLMResult(
            agent=kwargs["agent"],
            call_id=f"call_{len(self.requests)}",
            model="stub",
            assistant_message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.raw_arguments},
                    }
                ],
            },
            tool_calls=[call],
            finish_reason="tool_calls",
            usage={},
            cost={"source": "unavailable"},
            latency_s=0.01,
            attempts=1,
            transcript_ref="llm/smart_eg/call.md",
            diagnostics_ref="llm/smart_eg/call.diagnostics.json",
            provider_metadata={},
        )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        llm=LLMSettings(base_url="http://stub.invalid", api_key="stub", model="stub"),
        paths=Paths(
            repo_root=tmp_path,
            bird_root=tmp_path,
            proposals=tmp_path / "proposals",
            agent_prompts=tmp_path / "proposals" / "agent_prompts",
            schemas=tmp_path / "schemas",
            runs=tmp_path / "runs",
            dataset_out=tmp_path / "dataset",
        ),
        mongo_uri="mongodb://stub.invalid",
        stub=True,
        run_id="smart-eg-runtime",
    )


def _workflow(tmp_path: Path, llm: _LLM) -> SimpleNamespace:
    settings = _settings(tmp_path)
    log = setup_logging(settings.run_dir, console=False)
    ctx = SimpleNamespace(
        settings=settings,
        llm=llm,
        log=log,
        progress=_Progress(),
        mongo=_Mongo(),
        source=None,
        extra={},
    )
    return SimpleNamespace(ctx=ctx)


def test_submit_final_mql_is_only_success_exit(tmp_path: Path) -> None:
    llm = _LLM(
        [
            ToolCall(
                id="call_1",
                name="submit_final_mql",
                raw_arguments=(
                    '{"collection":"account","pipeline":[{"$limit":2}],'
                    '"MQL":"db.account.aggregate([{\\"$limit\\":2}])"}'
                ),
                arguments={
                    "collection": "account",
                    "pipeline": [{"$limit": 2}],
                    "MQL": 'db.account.aggregate([{"$limit":2}])',
                },
            )
        ]
    )
    result = asyncio.run(
        smart_solve_nlq_db_eg(
            _workflow(tmp_path, llm),
            db_id="financial",
            nlq="list accounts",
            record_id=1,
            policy=SmartEGPolicy(max_tool_turns=4),
        )
    )

    assert isinstance(result, SmartEGPrediction)
    assert result.result_type == "solver_prediction"
    assert result.record_id == 1
    assert result.MQL.startswith("db.account.aggregate")
    assert result.agent_session_ref.startswith("agent/")
    assert result.evidence_ledger_ref == "evidence_ledger.jsonl"


def test_abandon_with_failure_is_normal_failure_exit(tmp_path: Path) -> None:
    llm = _LLM(
        [
            ToolCall(
                id="call_1",
                name="abandon_with_failure",
                raw_arguments='{"error_code":"NO_VALID_QUERY_FOUND","message":"ambiguous"}',
                arguments={"error_code": "NO_VALID_QUERY_FOUND", "message": "ambiguous"},
            )
        ]
    )
    result = asyncio.run(
        smart_solve_nlq_db_eg(
            _workflow(tmp_path, llm),
            db_id="financial",
            nlq="ambiguous",
            record_id=9,
            policy=SmartEGPolicy(max_tool_turns=4),
        )
    )

    assert isinstance(result, SmartEGFailure)
    assert result.result_type == "solver_failure"
    assert result.record_id == 9
    assert result.error_code == "NO_VALID_QUERY_FOUND"
    assert result.agent_session_ref.startswith("agent/")


def test_natural_language_response_never_counts_as_success(tmp_path: Path) -> None:
    llm = _LLM(
        [
            None,
            ToolCall(
                id="call_2",
                name="abandon_with_failure",
                raw_arguments='{"message":"protocol recovered"}',
                arguments={"message": "protocol recovered"},
            ),
        ]
    )
    result = asyncio.run(
        smart_solve_nlq_db_eg(
            _workflow(tmp_path, llm),
            db_id="financial",
            nlq="answer without tools",
            policy=SmartEGPolicy(max_tool_turns=4),
        )
    )

    assert isinstance(result, SmartEGFailure)
    assert len(llm.requests) == 2


def test_terminal_only_mode_exposes_current_stage_submit_tools(tmp_path: Path) -> None:
    llm = _LLM(
        [
            ToolCall(
                id="call_1",
                name="abandon_with_failure",
                raw_arguments='{"message":"budget window"}',
                arguments={"message": "budget window"},
            )
        ]
    )
    result = asyncio.run(
        smart_solve_nlq_db_eg(
            _workflow(tmp_path, llm),
            db_id="financial",
            nlq="terminal",
            policy=SmartEGPolicy(max_tool_turns=2),
        )
    )

    assert isinstance(result, SmartEGFailure)
    exposed = {tool["function"]["name"] for tool in llm.requests[0]["tools"]}
    assert exposed == {"submit_environment_model", "abandon_with_failure"}
    assert llm.requests[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_environment_model"},
    }


def test_prompt_and_tool_schemas_guide_stage_progression() -> None:
    assert "submit_environment_model" in SYSTEM_PROMPT
    assert "submit_intent_hypothesis" in SYSTEM_PROMPT
    assert "submit_query_plan" in SYSTEM_PROMPT
    assert "read_documents" in SYSTEM_PROMPT and "not available" in SYSTEM_PROMPT

    schemas = {tool["function"]["name"]: tool["function"] for tool in tool_schemas()}

    sample = schemas["sample_documents"]
    assert "compact" in sample["description"].lower()
    assert "collection" in sample["parameters"]["properties"]

    submit_environment = schemas["submit_environment_model"]
    assert "candidate_collections" in submit_environment["parameters"]["properties"]

    submit_final = schemas["submit_final_mql"]
    assert {"collection", "pipeline", "MQL"}.issubset(
        submit_final["parameters"]["properties"]
    )

    readonly_probe = schemas["run_readonly_probe"]
    assert {"collection", "pipeline", "MQL"}.issubset(
        readonly_probe["parameters"]["properties"]
    )
