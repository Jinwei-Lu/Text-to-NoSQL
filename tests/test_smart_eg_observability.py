from __future__ import annotations

import json

import pytest

from tend.observability import setup_logging
from tend.solver.eg import SmartEGHistory, SmartEGPolicy, SmartEGState, SmartEGToolAPI
from tend.solver.eg.observability import SmartEGRecorder, build_session_id


class _Mongo:
    def list_collections(self, _db_id):
        return ["account"]

    def sample_documents(self, _db_id, collection, limit=3, **_kwargs):
        assert collection == "account"
        return [{"_id": 1}, {"_id": 2}][:limit]


def test_recorder_writes_agent_evidence_gate_cost_and_error_artifacts(tmp_path) -> None:
    log = setup_logging(tmp_path, console=False)
    recorder = SmartEGRecorder(log, session_id="smart-eg-financial-manual-deadbeef")

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
    recorder.write_cost_summary({"source": "unavailable", "total_tokens": 0})
    recorder.write_error({"error_code": "NO_VALID_QUERY_FOUND", "message": "x"})
    recorder.final_markdown("done")

    assert evidence_ref == "evidence_ledger.jsonl"
    assert gate_ref == "submit_gates.jsonl"
    assert (tmp_path / "agent" / "smart-eg-financial-manual-deadbeef.jsonl").exists()
    assert (tmp_path / "agent" / "smart-eg-financial-manual-deadbeef.md").exists()
    assert (tmp_path / "evidence_ledger.jsonl").exists()
    assert (tmp_path / "submit_gates.jsonl").exists()
    assert (tmp_path / "cost_summary.jsonl").exists()
    assert (tmp_path / "errors.jsonl").exists()
    rows = [
        json.loads(line)
        for line in (tmp_path / "agent" / "smart-eg-financial-manual-deadbeef.jsonl")
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
    recorder = SmartEGRecorder(log, session_id="smart-eg-financial-manual-deadbeef")

    recorder.agent_event(
        "llm_response",
        {
            "turn_index": 1,
            "call_id": "call-1",
            "transcript_ref": "llm/smart_eg/call-1.diagnostics.json",
            "diagnostics_ref": "llm/smart_eg/call-1.diagnostics.json",
            "has_tool_calls": True,
            "tool_call_count": 1,
        },
    )
    recorder.agent_event(
        "tool_call",
        {
            "turn_index": 1,
            "tool_call_id": "tool-1",
            "tool": "list_collections",
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

    md = (tmp_path / "agent" / "smart-eg-financial-manual-deadbeef.md").read_text(
        encoding="utf-8"
    )

    assert "# Agent Session: smart-eg-financial-manual-deadbeef" in md
    assert "Status: running" in md
    assert "## Turn 1" in md
    assert "### Reasoning" in md
    assert "### LLM Call" in md
    assert "### Tool Calls" in md
    assert "### Tool Results" in md
    assert "### Metrics" in md
    assert "llm/smart_eg/call-1.diagnostics.json" in md
    assert "Markdown Transcript" not in md
    assert "#### list_collections (`tool-1`)" in md
    assert "#### list_collections() (`tool-1`)" in md
    assert '"collections": [' in md
    assert "### LLM Response" not in md
    assert "### Tool Call:" not in md


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
    assert terminal_tools == {"submit_query_plan", "abandon_with_failure"}
    assert api.tool_choice_for_state(state) == {
        "type": "function",
        "function": {"name": "submit_query_plan"},
    }

    state.mode = "execution"
    terminal_tools = {tool["function"]["name"] for tool in api.tools_for_state(state)}
    assert terminal_tools == {"submit_final_mql", "abandon_with_failure"}
    assert api.tool_choice_for_state(state) == {
        "type": "function",
        "function": {"name": "submit_final_mql"},
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

    assert "submit_query_plan" in terminal_tools
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


def test_execution_ready_narrows_to_final_submit_without_protocol_penalty() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), db_handle=_Mongo())
    state = SmartEGState(nlq="list accounts", db_id="financial", mode="execution")
    state.environment = {"candidate_collections": ["account"]}
    state.intent = {"task_kind": "aggregation"}
    state.query_plan = {"collection": "account", "stages": [{"$limit": 1}]}

    exposed_names = {tool["function"]["name"] for tool in api.tools_for_state(state)}

    assert exposed_names == {
        "submit_final_mql",
        "inspect_evidence_ledger",
        "inspect_evidence_debt",
        "abandon_with_failure",
    }
    assert api.tool_choice_for_state(state) == {
        "type": "function",
        "function": {"name": "submit_final_mql"},
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
