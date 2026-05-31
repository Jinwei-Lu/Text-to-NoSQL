"""Tests for LLM transcript logging."""

from __future__ import annotations

import json

from tend.core import logging as log_module
from tend.core.llm_client import LLMClient, get_pool


def test_llm_transcript_logs_prompt_and_response(tmp_path):
    run_dir = tmp_path / "run"
    log_module.init_run_dir(run_dir)
    log_module.bind(db_id="orchestra", record_id=1001, agent="WP")

    client = LLMClient(stub=True, use_cache=False)
    result = client.call("B_rtv", "Say hello in JSON", seed=7)

    transcript_path = run_dir / "llm_transcript.jsonl"
    assert transcript_path.exists()
    lines = transcript_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    record = json.loads(lines[-1])
    assert record["event"] == "llm.transcript"
    assert record["prompt"] == "Say hello in JSON"
    assert record["pool"] == "B_rtv"
    assert record["db_id"] == "orchestra"
    assert record["record_id"] == 1001
    assert record["agent"] == "WP"
    assert record["stub"] is True
    assert "response_raw" in record
    assert result == record["response"]


def test_cache_hit_also_writes_transcript(tmp_path):
    run_dir = tmp_path / "run2"
    cache_dir = tmp_path / "cache"
    log_module.init_run_dir(run_dir)

    client = LLMClient(stub=False, use_cache=True, cache_dir=cache_dir)
    model = get_pool("A_construct").models[0]
    cache_key = client._cache_key("A_construct", "first call", 1, model)
    (cache_dir / f"{cache_key}.json").write_text('{"answer": "cached"}', encoding="utf-8")

    client.call("A_construct", "first call", seed=1)

    lines = (run_dir / "llm_transcript.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["cache_hit"] is True
    assert record["prompt"] == "first call"
    assert record["response"] == {"answer": "cached"}
