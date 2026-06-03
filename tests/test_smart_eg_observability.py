from __future__ import annotations

import json

import pytest

from tend.observability import setup_logging
from tend.solver.eg import SmartEGHistory, SmartEGPolicy, SmartEGState, SmartEGToolAPI
from tend.solver.eg.observability import SmartEGRecorder


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
                    {"id": f"later_{idx}", "type": "function", "function": {"name": "inspect_evidence_ledger"}}
                ],
            }
        )
        history.add_tool_result(f"later_{idx}", "inspect_evidence_ledger", {"claims": []})

    assert history.compact(max_messages=8, state_summary={"mode": "execution"}) is True
    assert len(history.messages) <= 8
    assert history.validate_provider_invariants() is True


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

    state.terminal_only = True
    terminal_tools = {tool["function"]["name"] for tool in api.tools_for_state(state)}
    assert terminal_tools == {"submit_final_mql", "abandon_with_failure"}
    assert api.tool_choice_for_state(state) == {
        "type": "function",
        "function": {"name": "abandon_with_failure"},
    }
