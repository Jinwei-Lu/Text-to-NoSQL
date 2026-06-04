from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from tend.config import LLMSettings, Paths, Settings
from tend.llm.types import ToolCall, ToolLLMResult
from tend.observability import setup_logging
from tend.solver.eg import SmartEGPolicy, smart_solve_nlq_db_eg
from tend.solver.eg.contracts import SmartEGFailure, SmartEGPrediction
from tend.solver.eg.runtime import (
    SYSTEM_PROMPT,
    _compact_evidence_summary,
    _single_exposed_submit_tool,
    _submit_focus_summary,
)
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
                transcript_ref="llm/smart_eg/call.diagnostics.json",
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
            transcript_ref="llm/smart_eg/call.diagnostics.json",
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


def test_direct_first_turn_submit_final_mql_is_rejected(tmp_path: Path) -> None:
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
            ),
            ToolCall(
                id="call_2",
                name="abandon_with_failure",
                raw_arguments='{"message":"direct final was rejected"}',
                arguments={"message": "direct final was rejected"},
            ),
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

    assert isinstance(result, SmartEGFailure)
    assert result.result_type == "solver_failure"
    assert result.record_id == 1
    assert result.agent_session_ref.startswith("agent/")
    assert Path(result.agent_session_ref).name.startswith(
        "solve_smart_eg_financial_record_1_"
    )
    assert result.evidence_ledger_ref == "evidence_ledger.jsonl"
    agent_jsonl = _settings(tmp_path).run_dir / result.agent_session_ref.replace(".md", ".jsonl")
    rows = [json.loads(line) for line in agent_jsonl.read_text(encoding="utf-8").splitlines()]
    final_observation = next(
        row
        for row in rows
        if row.get("event") == "tool_observation" and row.get("tool") == "submit_final_mql"
    )
    assert final_observation["ok"] is False
    assert final_observation["content"]["ok"] is False
    assert final_observation["content"]["reason"] == "tool_not_exposed"


def test_full_staged_submit_final_mql_succeeds_with_refs_and_executor(tmp_path: Path) -> None:
    llm = _LLM(
        [
            ToolCall(id="call_1", name="list_collections", raw_arguments="{}", arguments={}),
            ToolCall(
                id="call_2",
                name="discover_paths",
                raw_arguments='{"collection":"account","limit":3}',
                arguments={"collection": "account", "limit": 3},
            ),
            ToolCall(
                id="call_3",
                name="submit_environment_model",
                raw_arguments=(
                    '{"candidate_collections":["account"],'
                    '"evidence_refs":["ev-0001","ev-0002"]}'
                ),
                arguments={
                    "candidate_collections": ["account"],
                    "evidence_refs": ["ev-0001", "ev-0002"],
                },
            ),
            ToolCall(
                id="call_4",
                name="submit_intent_hypothesis",
                raw_arguments=(
                    '{"task_kind":"list","target_collection":"account",'
                    '"target_fields":["_id"],"evidence_refs":["ev-0002"]}'
                ),
                arguments={
                    "task_kind": "list",
                    "target_collection": "account",
                    "target_fields": ["_id"],
                    "evidence_refs": ["ev-0002"],
                },
            ),
            ToolCall(
                id="call_5",
                name="submit_query_plan",
                raw_arguments=(
                    '{"collection":"account","stages":[{"$limit":2}],'
                    '"evidence_refs":["ev-0002"]}'
                ),
                arguments={
                    "collection": "account",
                    "stages": [{"$limit": 2}],
                    "evidence_refs": ["ev-0002"],
                },
            ),
            ToolCall(
                id="call_6",
                name="run_readonly_probe",
                raw_arguments='{"collection":"account","pipeline":[{"$limit":2}]}',
                arguments={"collection": "account", "pipeline": [{"$limit": 2}]},
            ),
            ToolCall(
                id="call_7",
                name="submit_final_mql",
                raw_arguments=(
                    '{"collection":"account","pipeline":[{"$limit":2}],'
                    '"MQL":"db.account.aggregate([{\\"$limit\\":2}])",'
                    '"evidence_refs":["ev-0003"]}'
                ),
                arguments={
                    "collection": "account",
                    "pipeline": [{"$limit": 2}],
                    "MQL": 'db.account.aggregate([{"$limit":2}])',
                    "evidence_refs": ["ev-0003"],
                },
            ),
        ]
    )

    result = asyncio.run(
        smart_solve_nlq_db_eg(
            _workflow(tmp_path, llm),
            db_id="financial",
            nlq="list accounts",
            record_id=2,
            policy=SmartEGPolicy(max_tool_turns=12),
        )
    )

    assert isinstance(result, SmartEGPrediction)
    assert result.result_type == "solver_prediction"
    assert result.record_id == 2
    assert result.MQL.startswith("db.account.aggregate")
    assert result.submit_gate_refs


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


def test_terminal_only_known_wrong_tool_does_not_consume_tool_budget(tmp_path: Path) -> None:
    llm = _LLM(
        [
            ToolCall(
                id="call_1",
                name="sample_documents",
                raw_arguments='{"collection":"account"}',
                arguments={"collection": "account"},
            ),
            ToolCall(
                id="call_2",
                name="abandon_with_failure",
                raw_arguments='{"message":"provider ignored narrowed tools"}',
                arguments={"message": "provider ignored narrowed tools"},
            ),
        ]
    )
    result = asyncio.run(
        smart_solve_nlq_db_eg(
            _workflow(tmp_path, llm),
            db_id="financial",
            nlq="terminal",
            policy=SmartEGPolicy(max_tool_turns=1),
        )
    )

    assert isinstance(result, SmartEGFailure)
    assert result.error_code == "NO_VALID_QUERY_FOUND"
    assert len(llm.requests) == 2


def test_submit_ready_turn_compacts_provider_history(tmp_path: Path) -> None:
    llm = _LLM(
        [
            ToolCall(
                id="call_1",
                name="list_collections",
                raw_arguments="{}",
                arguments={},
            ),
            ToolCall(
                id="call_2",
                name="sample_documents",
                raw_arguments='{"collection":"account"}',
                arguments={"collection": "account"},
            ),
            ToolCall(
                id="call_3",
                name="submit_environment_model",
                raw_arguments=(
                    '{"candidate_collections":["account"],'
                    '"evidence_refs":["ev-0001","ev-0002"]}'
                ),
                arguments={
                    "candidate_collections": ["account"],
                    "evidence_refs": ["ev-0001", "ev-0002"],
                },
            ),
            ToolCall(
                id="call_4",
                name="abandon_with_failure",
                raw_arguments='{"message":"stop after compact check"}',
                arguments={"message": "stop after compact check"},
            ),
        ]
    )

    result = asyncio.run(
        smart_solve_nlq_db_eg(
            _workflow(tmp_path, llm),
            db_id="financial",
            nlq="list accounts",
            record_id=3,
            policy=SmartEGPolicy(max_tool_turns=8),
        )
    )

    submit_request = llm.requests[2]
    roles = [message["role"] for message in submit_request["messages"]]
    joined = "\n".join(str(message.get("content", "")) for message in submit_request["messages"])

    assert roles == ["system", "user"]
    assert '"required_next_tool": "submit_environment_model"' in joined
    assert submit_request["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_environment_model"},
    }
    md = (_settings(tmp_path).run_dir / result.agent_session_ref).read_text(
        encoding="utf-8"
    )
    assert "### LLM Call" in md
    assert "### Tool Calls" in md
    assert "### Tool Results" in md
    assert "### Metrics" in md
    assert md.count("#### list_collections()") == 1
    assert md.count('#### sample_documents(collection="account")') == 1
    assert "### Tool Result:" not in md


def test_submit_focus_summary_preserves_query_plan_for_final_submit() -> None:
    state = SimpleNamespace(
        nlq="list accounts",
        db_id="financial",
        record_id=7,
        mode="execution",
        terminal_only=False,
        terminal_reason=None,
        stale_milestones=set(),
        environment={"candidate_collections": ["account"]},
        intent={"task_kind": "aggregation"},
        query_plan={"collection": "account", "stages": [{"$limit": 1}]},
        evidence_ledger=SimpleNamespace(
            summary=lambda: {"evidence_records": 2, "blocking_debts": 0},
            records={},
            blocking_debts=lambda: [],
        ),
        counters=SimpleNamespace(to_json=lambda: {"llm_turns": 3, "tool_turns": 2}),
    )

    summary = _submit_focus_summary(state, "submit_final_mql")

    assert summary["required_next_tool"] == "submit_final_mql"
    assert summary["query_plan"] == {"collection": "account", "stages": [{"$limit": 1}]}


def test_submit_focus_summary_bounds_large_evidence_maps() -> None:
    summary = _compact_evidence_summary(
        {
            "tool": "sample_documents",
            "collection": "account",
            "path_count": 200,
            "paths": {f"path_{idx}": {"value_count": idx} for idx in range(100)},
            "dynamic_key_candidates": [{"path": f"path_{idx}"} for idx in range(100)],
            "array_paths": [f"arr_{idx}" for idx in range(20)],
        }
    )

    assert summary["tool"] == "sample_documents"
    assert summary["path_count"] == 200
    assert len(summary["array_paths"]) == 12
    assert "paths" not in summary
    assert "dynamic_key_candidates" not in summary


def test_submit_focus_detection_ignores_broad_exploration_tool_sets() -> None:
    assert (
        _single_exposed_submit_tool(
            {
                "submit_query_plan",
                "sample_documents",
                "run_readonly_probe",
                "inspect_evidence_ledger",
                "abandon_with_failure",
            }
        )
        is None
    )
    assert (
        _single_exposed_submit_tool(
            {
                "submit_query_plan",
                "inspect_evidence_ledger",
                "inspect_evidence_debt",
                "abandon_with_failure",
            }
        )
        == "submit_query_plan"
    )


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
