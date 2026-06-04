from __future__ import annotations

import json

import pytest

from tend.observability import setup_logging
from tend.solver.eg import SmartEGHistory, SmartEGPolicy, SmartEGState, SmartEGToolAPI
from tend.solver.eg.execution import run_final_sanity_execution
from tend.solver.eg.observability import SmartEGRecorder, build_session_id


class _Mongo:
    def list_collections(self, _db_id):
        return ["account"]

    def sample_documents(self, _db_id, collection, limit=3, **_kwargs):
        assert collection == "account"
        return [{"_id": 1}, {"_id": 2}][:limit]


def test_recorder_writes_session_scoped_artifacts(tmp_path) -> None:
    log = setup_logging(tmp_path, console=False)
    session_id = "smart-eg-financial-manual-deadbeef"
    recorder = SmartEGRecorder(log, session_id=session_id)
    session_ref = f"solve/sessions/{session_id}"
    session_dir = tmp_path / session_ref
    recorder.start_session(
        stage="solve",
        task="smart_eg",
        db_id="financial",
        tools=[{"type": "function", "function": {"name": "list_collections"}}],
    )

    recorder.agent_event("turn_start", mode="environment", tool_turn=0)
    evidence_ref = recorder.write_evidence(
        {
            "evidence_id": "ev_1",
            "source_tool": "list_collections",
            "tool_call_id": "call_1",
            "summary": {"collections": ["account"]},
        }
    )
    gate_ref = recorder.write_submit_gate(
        {"submit_tool": "submit_final_mql", "accepted": True, "milestone": "final"}
    )
    cost_ref = recorder.write_cost_summary({"source": "unavailable", "total_tokens": 0})
    error_ref = recorder.write_error({"error_code": "NO_VALID_QUERY_FOUND", "message": "x"})
    progress_ref = recorder.record_progress({"phase": "turn"})
    recorder.final_markdown("done")

    assert recorder.agent_ref() == f"{session_ref}/agent.md"
    assert recorder.agent_jsonl_ref() == f"{session_ref}/agent.jsonl"
    assert recorder.tools_ref() == f"{session_ref}/tools.json"
    assert evidence_ref == f"{session_ref}/evidence_ledger.jsonl"
    assert gate_ref == f"{session_ref}/submit_gates.jsonl"
    assert cost_ref == f"{session_ref}/cost_summary.jsonl"
    assert error_ref == f"{session_ref}/errors.jsonl"
    assert progress_ref == f"{session_ref}/progress.jsonl#1"
    assert (session_dir / "agent.jsonl").exists()
    assert (session_dir / "agent.md").exists()
    assert (session_dir / "tools.json").exists()
    assert (session_dir / "evidence_ledger.jsonl").exists()
    assert (session_dir / "submit_gates.jsonl").exists()
    assert (session_dir / "cost_summary.jsonl").exists()
    assert (session_dir / "errors.jsonl").exists()
    assert (session_dir / "progress.jsonl").exists()
    assert not (tmp_path / "agent").exists()
    for root_sidecar in [
        "evidence_ledger.jsonl",
        "submit_gates.jsonl",
        "execution_trace.jsonl",
        "progress.jsonl",
    ]:
        assert not (tmp_path / root_sidecar).exists()
    assert (tmp_path / "cost_summary.jsonl").exists()
    assert (tmp_path / "errors.jsonl").exists()
    root_errors = [
        json.loads(line)
        for line in (tmp_path / "errors.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert root_errors[0]["session_error_ref"] == f"{error_ref}#1"
    assert root_errors[0]["agent_session_ref"] == f"{session_ref}/agent.md"
    rows = [
        json.loads(line)
        for line in (session_dir / "agent.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[0]["event"] == "turn_start"


def test_default_session_id_includes_stage_task_database_and_record() -> None:
    session_id = build_session_id(
        stage="solve",
        task="smart_eg",
        db_id="financial",
        record_id=31131,
    )

    assert session_id.startswith("solve_smart_eg_financial_record_31131_")


def test_history_enforces_assistant_tool_pairs_and_compacts_safely() -> None:
    history = SmartEGHistory(system_prompt="system")
    history.add_user("start")
    history.add_assistant(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "list_collections"}},
                {"id": "call_2", "type": "function", "function": {"name": "inspect_evidence_debt"}},
            ],
        }
    )
    history.add_tool_result("call_1", "list_collections", {"collections": ["account"]})
    with pytest.raises(ValueError, match="pending tool calls"):
        history.add_user("invalid interruption")
    history.add_tool_result("call_2", "inspect_evidence_debt", {"debts": []})

    for idx in range(6):
        history.add_user(f"turn {idx}")
        history.add_assistant(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"later_{idx}",
                        "type": "function",
                        "function": {"name": "inspect_evidence_ledger"},
                    }
                ],
            }
        )
        history.add_tool_result(f"later_{idx}", "inspect_evidence_ledger", {"claims": []})

    assert history.compact(max_messages=8, state_summary={"mode": "execution"}) is True
    assert len(history.messages) <= 8
    assert history.validate_provider_invariants() is True


def test_history_truncates_large_tool_results_before_provider_prompt() -> None:
    history = SmartEGHistory(system_prompt="system")
    history.add_user("start")
    history.add_assistant(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "big_1", "type": "function", "function": {"name": "sample_documents"}}
            ],
        }
    )
    history.add_tool_result(
        "big_1",
        "sample_documents",
        {"observation": {"large": "x" * 200_000}, "evidence_id": "ev_1"},
    )

    tool_message = history.messages[-1]

    assert tool_message["role"] == "tool"
    assert len(tool_message["content"]) < 15_000
    assert "truncated_for_prompt" in tool_message["content"]
    assert "ev_1" in tool_message["content"]
    assert history.validate_provider_invariants() is True


def test_history_does_not_append_runtime_state_to_provider_prompt() -> None:
    history = SmartEGHistory(system_prompt="system")
    history.add_user("NLQ: list accounts\nDB: financial\nRecord: 1")

    messages = history.build_messages(
        {
            "mode": "environment",
            "budgets": {"max_tool_turns": 48},
            "counters": {"llm_turns": 0},
        }
    )
    joined = "\n".join(str(message.get("content", "")) for message in messages)

    assert messages == history.messages
    assert "Runtime state" not in joined
    assert "budgets" not in joined
    assert "counters" not in joined


def test_recorder_keeps_live_markdown_with_llm_turn_and_tool_observation(tmp_path) -> None:
    log = setup_logging(tmp_path, console=False)
    session_id = "smart-eg-financial-manual-deadbeef"
    recorder = SmartEGRecorder(log, session_id=session_id)
    tool_schema = {
        "type": "function",
        "function": {
            "name": "list_collections",
            "parameters": {"type": "object", "description": "x" * 13_000},
        },
    }
    recorder.start_session(
        stage="solve",
        task="smart_eg",
        model="deepseek-v4-flash",
        system_prompt="# SMART-EG Solver",
        user_message="Task input:\nNLQ: list accounts\nDatabase: financial",
        tools=[tool_schema],
    )

    recorder.agent_event(
        "llm_request",
        {
            "turn_index": 1,
            "mode": "environment",
            "tools": ["list_collections"],
            "tool_schemas": [tool_schema],
            "tool_choice": "required",
            "messages": [{"role": "user", "content": "NLQ: list accounts"}],
        },
    )
    recorder.agent_event(
        "llm_response",
        {
            "turn_index": 1,
            "call_id": "call-1",
            "transcript_ref": "llm/smart_eg/call-1.diagnostics.json",
            "diagnostics_ref": "llm/smart_eg/call-1.diagnostics.json",
            "has_tool_calls": True,
            "tool_call_count": 1,
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            "cost": {"cost_usd": 0.001, "cost_source": "api"},
            "assistant_message": {
                "role": "assistant",
                "reasoning_content": "I need to inspect the database first.",
                "content": "Calling the collection listing tool.",
                "tool_calls": [
                    {
                        "id": "tool-1",
                        "type": "function",
                        "function": {"name": "list_collections", "arguments": "{}"},
                        "provider_extra": "preserved",
                    }
                ],
            },
        },
    )
    recorder.agent_event(
        "tool_call",
        {
            "turn_index": 1,
            "tool_call_id": "tool-1",
            "tool": "list_collections",
            "raw_tool_call": {
                "id": "tool-1",
                "type": "function",
                "function": {"name": "list_collections", "arguments": "{}"},
                "provider_extra": "preserved",
            },
            "arguments": {},
        },
    )
    recorder.agent_event(
        "tool_observation",
        {
            "turn_index": 1,
            "tool_call_id": "tool-1",
            "tool": "list_collections",
            "ok": True,
            "content": {"ok": True, "tool": "list_collections", "collections": ["account"]},
        },
    )

    md = (tmp_path / "solve" / "sessions" / session_id / "agent.md").read_text(encoding="utf-8")

    assert "# Agent Session: smart-eg-financial-manual-deadbeef" in md
    assert "| Stage | solve |" in md
    assert "| Task | smart_eg |" in md
    assert "| Model | deepseek-v4-flash |" in md
    assert "## System Prompt" in md
    assert "## User Message" in md
    assert "## Tools" in md
    assert "## Turn 1" in md
    assert "### Reasoning" in md
    assert "> I need to inspect the database first." in md
    assert "### Content" in md
    assert "> Calling the collection listing tool." in md
    assert "NLQ: list accounts" in md
    assert "truncated" not in md
    assert "x" * 500 in md
    assert "### Tool Calls" in md
    assert "### Tool Results" in md
    assert "### Metrics" in md
    assert "#### list_collections (`tool-1`)" in md
    assert "#### list_collections() (`tool-1`)" in md
    assert "### Tool Result: `list_collections`" in md
    assert '"collections": [' in md
    assert "| Prompt Tokens | 12 |" in md
    assert "| Completion Tokens | 3 |" in md
    assert "| Cost (USD) | 0.001 |" in md
    assert "Status: running" not in md
    assert "### LLM Call" not in md
    assert "#### Provider Request Messages" not in md
    assert "#### Provider Tool Schemas" not in md
    assert "#### Provider Assistant Message" not in md
    assert "Markdown Transcript" not in md
    assert "### LLM Response" not in md
    assert "### Tool Call:" not in md


def test_record_error_refs_are_visible_in_turn_markdown(tmp_path) -> None:
    log = setup_logging(tmp_path, console=False)
    session_id = "smart-eg-financial-manual-deadbeef"
    recorder = SmartEGRecorder(log, session_id=session_id)
    session_ref = f"solve/sessions/{session_id}"
    recorder.set_current_turn(1)
    recorder.agent_event(
        "tool_call",
        {
            "turn_index": 1,
            "tool_call_id": "tool-1",
            "tool": "list_collections",
            "arguments": {},
        },
    )

    error_ref = recorder.record_error(
        {
            "error_code": "TOOL_EXECUTION_FAILED",
            "tool": "list_collections",
            "message": "catalog unavailable",
        }
    )
    recorder.agent_event(
        "tool_observation",
        {
            "turn_index": 1,
            "tool_call_id": "tool-1",
            "tool": "list_collections",
            "ok": False,
            "error_refs": [error_ref],
            "content": {
                "ok": False,
                "tool": "list_collections",
                "reason": "tool_execution_failed",
                "error_refs": [error_ref],
            },
        },
    )

    md = (tmp_path / session_ref / "agent.md").read_text(encoding="utf-8")

    assert error_ref == f"{session_ref}/errors.jsonl#1"
    assert (tmp_path / session_ref / "errors.jsonl").exists()
    assert (tmp_path / "errors.jsonl").exists()
    assert "## Turn 1" in md
    assert "Error Refs" in md
    assert error_ref in md
    assert "catalog unavailable" in md


def test_mode_based_tool_exposure_and_terminal_only_allowlist() -> None:
    api = SmartEGToolAPI(SmartEGPolicy())
    state = SmartEGState(nlq="list accounts", db_id="financial")

    env_tools = {tool["function"]["name"] for tool in api.tools_for_state(state)}
    assert "list_collections" in env_tools
    assert "submit_environment_model" in env_tools
    assert "submit_final_mql" not in env_tools

    state.mode = "execution"
    exec_tools = {tool["function"]["name"] for tool in api.tools_for_state(state)}
    assert "render_pipeline" in exec_tools
    assert "submit_final_mql" in exec_tools
    assert "sample_documents" in exec_tools
    assert "profile_path_values" in exec_tools
    assert "run_readonly_probe" in exec_tools

    state.terminal_only = True
    state.mode = "planning"
    terminal_tools = {tool["function"]["name"] for tool in api.tools_for_state(state)}
    assert terminal_tools == {"abandon_with_failure"}
    assert api.tool_choice_for_state(state) == {
        "type": "function",
        "function": {"name": "abandon_with_failure"},
    }

    state.mode = "execution"
    terminal_tools = {tool["function"]["name"] for tool in api.tools_for_state(state)}
    assert terminal_tools == {"abandon_with_failure"}
    assert api.tool_choice_for_state(state) == {
        "type": "function",
        "function": {"name": "abandon_with_failure"},
    }

    forced_api = SmartEGToolAPI(SmartEGPolicy(force_tool_choice=True))
    assert forced_api.tool_choice_for_state(state) == api.tool_choice_for_state(state)


def test_terminal_only_with_blocking_debt_exposes_debt_repair_tools() -> None:
    api = SmartEGToolAPI(SmartEGPolicy())
    state = SmartEGState(nlq="compare loan-account share", db_id="financial")
    state.mode = "planning"
    state.terminal_only = True
    state.evidence_ledger.ensure_debt(
        milestone="plan",
        claim_type="value_grounding",
        missing_evidence=["literal:present"],
        suggested_tools=["profile_path_values", "search_values", "run_readonly_probe"],
    )

    terminal_tools = {tool["function"]["name"] for tool in api.tools_for_state(state)}

    assert "submit_query_plan" not in terminal_tools
    assert "abandon_with_failure" in terminal_tools
    assert "inspect_evidence_debt" in terminal_tools
    assert "profile_path_values" in terminal_tools
    assert "search_values" in terminal_tools
    assert "run_readonly_probe" in terminal_tools
    assert "sample_documents" not in terminal_tools
    assert api.tool_choice_for_state(state) is None


def test_environment_mode_narrows_to_submit_after_sufficient_evidence() -> None:
    api = SmartEGToolAPI(SmartEGPolicy())
    state = SmartEGState(nlq="list accounts", db_id="financial")

    state.evidence_ledger.add_record(
        source_tool="list_collections",
        tool_call_id="call_1",
        observation_ref="agent/session.jsonl#1",
        summary={"collections": ["account"]},
    )
    state.evidence_ledger.add_record(
        source_tool="sample_documents",
        tool_call_id="call_2",
        observation_ref="agent/session.jsonl#2",
        summary={"collection": "account", "path_count": 4},
    )

    env_tools = {tool["function"]["name"] for tool in api.tools_for_state(state)}

    assert "submit_environment_model" in env_tools
    assert "sample_documents" not in env_tools
    assert "discover_paths" not in env_tools
    assert "run_readonly_probe" not in env_tools
    assert "inspect_evidence_ledger" in env_tools


def test_environment_ready_probe_request_becomes_submit_required_feedback() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=_Mongo())
    state = SmartEGState(nlq="list accounts", db_id="financial")
    state.evidence_ledger.add_record(
        source_tool="list_collections",
        tool_call_id="call_1",
        observation_ref="agent/session.jsonl#1",
        summary={"collections": ["account"]},
    )
    state.evidence_ledger.add_record(
        source_tool="sample_documents",
        tool_call_id="call_2",
        observation_ref="agent/session.jsonl#2",
        summary={"collection": "account", "path_count": 4},
    )
    exposed_names = {tool["function"]["name"] for tool in api.tools_for_state(state)}

    observation = api.execute(
        {
            "id": "call_3",
            "function": {"name": "sample_documents", "arguments": '{"collection":"account"}'},
        },
        state,
        exposed_tool_names=exposed_names,
    )

    assert observation.ok is False
    assert observation.llm_visible_content["reason"] == "environment_ready_to_submit"
    assert observation.llm_visible_content["required_tool"] == "submit_environment_model"
    assert state.counters.protocol_violations == 0


def test_intent_ready_narrows_to_submit_without_protocol_penalty() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=_Mongo())
    state = SmartEGState(nlq="list accounts", db_id="financial", mode="intent")
    state.environment = {"candidate_collections": ["account"]}
    state.evidence_ledger.add_record(
        source_tool="profile_path_values",
        tool_call_id="call_1",
        observation_ref="agent/session.jsonl#1",
        summary={"collection": "account", "path": "frequency"},
    )
    exposed_names = {tool["function"]["name"] for tool in api.tools_for_state(state)}

    assert exposed_names == {
        "submit_intent_hypothesis",
        "inspect_evidence_ledger",
        "inspect_evidence_debt",
        "abandon_with_failure",
    }
    assert api.tool_choice_for_state(state) == {
        "type": "function",
        "function": {"name": "submit_intent_hypothesis"},
    }

    observation = api.execute(
        {
            "id": "call_2",
            "function": {
                "name": "profile_path_values",
                "arguments": '{"collection":"account","path":"x"}',
            },
        },
        state,
        exposed_tool_names=exposed_names,
    )

    assert observation.ok is False
    assert observation.llm_visible_content["reason"] == "intent_ready_to_submit"
    assert observation.llm_visible_content["required_tool"] == "submit_intent_hypothesis"
    assert state.counters.protocol_violations == 0


def test_tool_choice_never_names_tool_outside_current_exposure() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=_Mongo())
    state = SmartEGState(nlq="list accounts", db_id="financial", mode="intent")
    state.environment = {"candidate_collections": ["account"]}
    state.evidence_ledger.add_record(
        source_tool="list_collections",
        tool_call_id="call_1",
        observation_ref="agent/session.jsonl#1",
        summary={"collections": ["account"]},
    )
    state.evidence_ledger.add_record(
        source_tool="sample_documents",
        tool_call_id="call_2",
        observation_ref="agent/session.jsonl#2",
        summary={"collection": "account", "path_count": 4},
    )

    exposed_names = {tool["function"]["name"] for tool in api.tools_for_state(state)}
    tool_choice = api.tool_choice_for_state(state)

    if isinstance(tool_choice, dict):
        assert tool_choice["function"]["name"] in exposed_names


def test_planning_ready_narrows_to_submit_without_protocol_penalty() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=_Mongo())
    state = SmartEGState(nlq="list accounts", db_id="financial", mode="planning")
    state.intent = {"task_kind": "aggregation"}
    state.evidence_ledger.add_record(
        source_tool="discover_paths",
        tool_call_id="call_1",
        observation_ref="agent/session.jsonl#1",
        summary={"collection": "account", "paths": ["_id"]},
    )
    exposed_names = {tool["function"]["name"] for tool in api.tools_for_state(state)}

    assert exposed_names == {
        "submit_query_plan",
        "inspect_evidence_ledger",
        "inspect_evidence_debt",
        "abandon_with_failure",
    }
    assert api.tool_choice_for_state(state) == {
        "type": "function",
        "function": {"name": "submit_query_plan"},
    }

    observation = api.execute(
        {
            "id": "call_2",
            "function": {
                "name": "run_readonly_probe",
                "arguments": '{"collection":"account","pipeline":[]}',
            },
        },
        state,
        exposed_tool_names=exposed_names,
    )

    assert observation.ok is False
    assert observation.llm_visible_content["reason"] == "planning_ready_to_submit"
    assert observation.llm_visible_content["required_tool"] == "submit_query_plan"
    assert state.counters.protocol_violations == 0


def test_planning_ready_accepts_readonly_probe_as_plan_evidence() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=_Mongo())
    state = SmartEGState(nlq="list accounts", db_id="financial", mode="planning")
    state.intent = {"task_kind": "aggregation"}
    state.evidence_ledger.add_record(
        source_tool="run_readonly_probe",
        tool_call_id="call_1",
        observation_ref="agent/session.jsonl#1",
        summary={"ok": True},
    )

    exposed_names = {tool["function"]["name"] for tool in api.tools_for_state(state)}

    assert exposed_names == {
        "submit_query_plan",
        "inspect_evidence_ledger",
        "inspect_evidence_debt",
        "abandon_with_failure",
    }


def test_execution_mode_waits_for_final_evidence_before_submit_focus() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=_Mongo())
    state = SmartEGState(nlq="list accounts", db_id="financial", mode="execution")
    state.environment = {"candidate_collections": ["account"]}
    state.intent = {"task_kind": "aggregation"}
    state.query_plan = {"collection": "account", "stages": [{"$limit": 1}]}

    exposed_names = {tool["function"]["name"] for tool in api.tools_for_state(state)}

    assert "run_readonly_probe" in exposed_names
    assert "run_final_sanity_execution" in exposed_names
    assert api.tool_choice_for_state(state) is None

    state.evidence_ledger.add_record(
        source_tool="run_readonly_probe",
        tool_call_id="call_probe",
        observation_ref="agent/session.jsonl#probe",
        summary={"tool": "run_readonly_probe", "ok": True, "count": 1},
    )
    exposed_names = {tool["function"]["name"] for tool in api.tools_for_state(state)}

    assert "run_final_sanity_execution" in exposed_names
    assert api.tool_choice_for_state(state) is None

    state.evidence_ledger.add_record(
        source_tool="run_final_sanity_execution",
        tool_call_id="call_final",
        observation_ref="agent/session.jsonl#final",
        summary={"tool": "run_final_sanity_execution", "ok": True, "mql_hash": "sha256:x"},
    )
    exposed_names = {tool["function"]["name"] for tool in api.tools_for_state(state)}

    assert exposed_names == {
        "submit_final_mql",
        "inspect_evidence_ledger",
        "inspect_evidence_debt",
        "abandon_with_failure",
    }

    observation = api.execute(
        {
            "id": "call_1",
            "function": {
                "name": "run_readonly_probe",
                "arguments": '{"collection":"account","pipeline":[]}',
            },
        },
        state,
        exposed_tool_names=exposed_names,
    )

    assert observation.ok is False
    assert observation.llm_visible_content["reason"] == "execution_ready_to_submit"
    assert observation.llm_visible_content["required_tool"] == "submit_final_mql"
    assert state.counters.protocol_violations == 0


def test_check_ast_filter_accepts_pipeline_and_never_escapes_parse_error() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=_Mongo())
    state = SmartEGState(nlq="list accounts", db_id="financial", mode="execution")

    ok_observation = api.execute(
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "check_ast_filter",
                "arguments": json.dumps(
                    {"collection": "account", "pipeline": [{"$match": {"_id": {"$exists": True}}}]}
                ),
            },
        },
        state,
    )
    bad_observation = api.execute(
        {
            "id": "call_2",
            "type": "function",
            "function": {"name": "check_ast_filter", "arguments": "{}"},
        },
        state,
    )

    assert ok_observation.ok is True
    assert ok_observation.result["collection"] == "account"
    assert bad_observation.ok is False
    assert bad_observation.result["error_type"] in {"ExecutionError", "ResponseParseError"}


def test_check_ast_filter_rejects_canonical_disabled_system_var() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=_Mongo())
    state = SmartEGState(nlq="recent accounts", db_id="financial", mode="execution")

    observation = api.execute(
        {
            "id": "call_now",
            "type": "function",
            "function": {
                "name": "check_ast_filter",
                "arguments": json.dumps(
                    {"MQL": 'db.account.aggregate([{"$match":{"created_at":{"$lte":"$$NOW"}}}])'}
                ),
            },
        },
        state,
    )

    assert observation.ok is False
    assert observation.result["ok"] is False
    assert "$$NOW" in observation.result["disallowed_operators"]


def test_run_final_sanity_execution_requires_supported_executor() -> None:
    missing = run_final_sanity_execution(
        executor=None,
        db_id="financial",
        mql='db.account.aggregate([{"$limit":1}])',
    )
    unsupported = run_final_sanity_execution(
        executor=object(),
        db_id="financial",
        mql='db.account.aggregate([{"$limit":1}])',
    )

    assert missing["ok"] is False
    assert missing["reason"] == "no_executor"
    assert unsupported["ok"] is False
    assert unsupported["reason"] == "unsupported_executor"


def test_run_final_sanity_execution_preserves_bounded_executor_failure() -> None:
    class FailingBoundedExecutor:
        def aggregate_readonly_bounded(self, _db_id, _mql, limit=50):
            return {
                "ok": False,
                "error_class": "EXECUTION_ERROR",
                "error_type": "ValueError",
                "message": "bad pipeline",
                "count": 0,
                "sample": [],
            }

    result = run_final_sanity_execution(
        executor=FailingBoundedExecutor(),
        db_id="financial",
        mql='db.account.aggregate([{"$limit":1}])',
    )

    assert result["ok"] is False
    assert result["skipped"] is False
    assert result["error_type"] == "ValueError"
    assert result["error_class"] == "EXECUTION_ERROR"


def test_run_final_sanity_execution_rejects_disabled_operator_before_executor() -> None:
    class CountingExecutor:
        def __init__(self) -> None:
            self.calls = 0

        def norm_exec(self, _db_id, _mql):
            self.calls += 1
            raise AssertionError("disabled MQL must not execute")

    executor = CountingExecutor()

    result = run_final_sanity_execution(
        executor=executor,
        db_id="financial",
        mql='db.account.aggregate([{"$sample":{"size":1}}])',
    )

    assert result["ok"] is False
    assert result["reason"] == "disabled_operator"
    assert "$sample" in result["disallowed_operators"]
    assert executor.calls == 0


def test_readonly_mongo_tools_stay_available_across_non_terminal_modes() -> None:
    api = SmartEGToolAPI(SmartEGPolicy())
    state = SmartEGState(nlq="list accounts", db_id="financial")

    for mode in ["environment", "intent", "planning", "execution"]:
        state.mode = mode
        state.terminal_only = False
        names = {tool["function"]["name"] for tool in api.tools_for_state(state)}
        assert "sample_documents" in names
        assert "profile_path_values" in names
        assert "run_readonly_probe" in names


def test_no_value_grounding_policy_hides_value_tools_across_modes() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(value_grounding=False))
    state = SmartEGState(nlq="list accounts", db_id="financial")

    for mode in ["environment", "intent", "planning", "execution"]:
        state.mode = mode
        state.terminal_only = False
        names = {tool["function"]["name"] for tool in api.tools_for_state(state)}
        assert "profile_path_values" not in names
        assert "search_values" not in names
        assert "profile_path" in names
        assert "run_readonly_probe" in names


def test_tool_execution_uses_turn_exposure_snapshot() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=_Mongo())
    state = SmartEGState(nlq="list accounts", db_id="financial")
    exposed_names = {tool["function"]["name"] for tool in api.tools_for_state(state)}

    first = api.execute(
        {
            "id": "call_1",
            "function": {"name": "list_collections", "arguments": "{}"},
        },
        state,
        exposed_tool_names=exposed_names,
    )
    second = api.execute(
        {
            "id": "call_2",
            "function": {"name": "sample_documents", "arguments": '{"collection":"account"}'},
        },
        state,
        exposed_tool_names=exposed_names,
    )

    assert first.ok is True
    assert second.ok is True
    assert state.counters.protocol_violations == 0


def test_explicit_empty_exposure_snapshot_rejects_known_and_terminal_tools() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=_Mongo())
    state = SmartEGState(nlq="list accounts", db_id="financial")

    known = api.execute(
        {
            "id": "call_1",
            "function": {"name": "list_collections", "arguments": "{}"},
        },
        state,
        exposed_tool_names=set(),
    )
    terminal = api.execute(
        {
            "id": "call_2",
            "function": {
                "name": "submit_final_mql",
                "arguments": '{"collection":"account","pipeline":[{"$limit":1}]}',
            },
        },
        state,
        exposed_tool_names=set(),
    )

    assert known.ok is False
    assert known.llm_visible_content["reason"] == "tool_not_exposed"
    assert terminal.ok is False
    assert terminal.llm_visible_content["reason"] == "tool_not_exposed"


def test_prefix_tools_execute_with_prefix_executor() -> None:
    class PrefixExecutor:
        def __init__(self) -> None:
            self.requests = []

        def execute_prefix(self, request):
            from tend.solver.per_stage import PrefixExecutionResult

            self.requests.append(request)
            return PrefixExecutionResult.single_variant([{"_id": 1}])

    executor = PrefixExecutor()
    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=_Mongo(), executor=executor)
    state = SmartEGState(nlq="list accounts", db_id="financial", mode="execution")

    observation = api.execute(
        {
            "id": "call_prefix",
            "function": {
                "name": "execute_pipeline_prefix",
                "arguments": '{"collection":"account","pipeline":[{"$limit":1}],"prefix_length":1}',
            },
        },
        state,
    )

    assert observation.ok is True
    assert observation.result["prefix_length"] == 1
    assert observation.result["prefixes_executed"] == 1
    assert observation.result["evidence_id"] == "ev-0001"
    assert [request.stage_index for request in executor.requests] == [1]


def test_policy_flags_change_exposure_and_readiness() -> None:
    api = SmartEGToolAPI(
        SmartEGPolicy(
            counterexample_gate=False,
            relationship_probe=False,
            prefix_execution=False,
            revisit=False,
        ),
        db_handle=_Mongo(),
    )
    state = SmartEGState(nlq="list accounts", db_id="financial", mode="execution")
    names = {tool["function"]["name"] for tool in api.tools_for_state(state)}

    assert "mine_counterexamples" not in names
    assert "profile_relationship_candidates" not in names
    assert "request_revisit" not in names
    assert "render_pipeline_prefix" not in names
    assert "execute_pipeline_prefix" not in names
    assert "check_prefix_checkpoint" not in names

    disabled_counterexample = api.execute(
        {"id": "call_mine", "function": {"name": "mine_counterexamples", "arguments": "{}"}},
        state,
    )
    assert disabled_counterexample.ok is False
    assert disabled_counterexample.llm_visible_content["reason"] == "tool_not_exposed"

    env_state = SmartEGState(nlq="list accounts", db_id="financial")
    env_state.evidence_ledger.add_record(
        source_tool="list_collections",
        tool_call_id="call_1",
        observation_ref="agent/session.jsonl#1",
        summary={"collections": ["account"]},
    )
    env_state.evidence_ledger.add_record(
        source_tool="profile_relationship_candidates",
        tool_call_id="call_2",
        observation_ref="agent/session.jsonl#2",
        summary={"tool": "profile_relationship_candidates", "candidates": []},
    )

    env_names = {tool["function"]["name"] for tool in api.tools_for_state(env_state)}

    assert "sample_documents" in env_names
    assert "submit_environment_model" in env_names
    assert api.tool_choice_for_state(env_state) is None


def test_list_collections_passes_current_db_id_to_db_handle() -> None:
    class TrackingMongo:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def list_collections(self, db_id):
            self.calls.append(db_id)
            return ["account"]

    mongo = TrackingMongo()
    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=mongo)
    state = SmartEGState(nlq="list accounts", db_id="financial")

    observation = api.execute(
        {
            "id": "call_collections",
            "function": {"name": "list_collections", "arguments": "{}"},
        },
        state,
    )

    assert observation.ok is True
    assert mongo.calls == ["financial"]


def test_list_collections_error_is_failed_observation_without_evidence() -> None:
    class BrokenMongo:
        def list_collections(self, _db_id):
            raise RuntimeError("db unavailable")

    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=BrokenMongo())
    state = SmartEGState(nlq="list accounts", db_id="financial")

    observation = api.execute(
        {
            "id": "call_collections",
            "function": {"name": "list_collections", "arguments": "{}"},
        },
        state,
    )

    assert observation.ok is False
    assert observation.llm_visible_content["ok"] is False
    assert observation.result["reason"] == "tool_execution_failed"
    assert state.evidence_ledger.records == {}


def test_candidate_check_tools_are_not_exposed_as_live_tools() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=_Mongo())
    state = SmartEGState(nlq="list accounts", db_id="financial")

    for mode in ["environment", "intent", "planning", "execution"]:
        state.mode = mode
        names = {tool["function"]["name"] for tool in api.tools_for_state(state)}
        assert "check_environment_model" not in names
        assert "check_intent_hypothesis" not in names
        assert "check_query_plan" not in names
        assert "check_final_candidate" not in names
