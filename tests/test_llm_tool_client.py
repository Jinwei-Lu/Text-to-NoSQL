from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from tend.config import LLMSettings, Paths, Settings
from tend.errors import LLMError, RateLimitError
from tend.llm import LLMClient
from tend.llm.types import ToolCall, ToolLLMResult
from tend.observability import setup_logging


def _settings(tmp_path, *, stub: bool = True, max_retries: int = 1):
    return Settings(
        llm=LLMSettings(
            base_url="http://stub.invalid",
            api_key="stub",
            model="stub-model",
            max_retries=max_retries,
        ),
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
        stub=stub,
        run_id="tool-client",
    )


def _tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "list_collections",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _tool_call_dict(call_id: str = "call_1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "list_collections",
            "arguments": '{"db_id":"financial"}',
        },
    }


def _tool_response() -> tuple[str, str, dict[str, int], dict[str, Any], list[dict[str, Any]]]:
    return (
        "",
        "tool_calls",
        {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        {"id": "chatcmpl-tools"},
        [_tool_call_dict()],
    )


def _read_events(run_dir):
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


class _FakeStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = iter(chunks)

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeCompletions:
    def __init__(self, chunks: list[Any]) -> None:
        self.chunks = chunks
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        return _FakeStream(self.chunks)


class _FakeOpenAI:
    def __init__(self, chunks: list[Any]) -> None:
        self.completions = _FakeCompletions(chunks)
        self.chat = SimpleNamespace(completions=self.completions)


def _delta_chunk(*, tool_calls: list[Any] | None = None, finish_reason: str | None = None,
                 usage: Any = None) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        usage=usage,
    )


def test_complete_with_tools_returns_native_tool_calls(tmp_path) -> None:
    settings = _settings(tmp_path)
    log = setup_logging(settings.run_dir, console=False)
    client = LLMClient(settings, log)

    def stub(agent, messages, schema):
        assert agent == "smart_eg"
        assert messages[-1]["role"] == "user"
        return {
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "list_collections",
                        "arguments": '{"db_id":"financial"}',
                    },
                }
            ]
        }

    client.set_stub(stub)
    result = asyncio.run(
        client.complete_with_tools(
            agent="smart_eg",
            messages=[{"role": "user", "content": "inspect db"}],
            tools=[_tool_schema()],
            tool_choice={"type": "function", "function": {"name": "list_collections"}},
        )
    )

    assert isinstance(result, ToolLLMResult)
    assert result.tool_calls == [
        ToolCall(
            id="call_1",
            name="list_collections",
            arguments={"db_id": "financial"},
            raw_arguments='{"db_id":"financial"}',
        )
    ]
    assert result.assistant_message["tool_calls"][0]["function"]["name"] == "list_collections"
    assert result.cost["source"] in {"unavailable", "estimated", "api", "error"}
    assert result.transcript_ref.startswith("llm/smart_eg/")


def test_complete_with_tools_markdown_renders_full_request_and_tool_context(tmp_path) -> None:
    settings = _settings(tmp_path)
    log = setup_logging(settings.run_dir, console=False)
    client = LLMClient(settings, log)
    tool_choice = {"type": "function", "function": {"name": "list_collections"}}

    def stub(agent, messages, schema):
        assert agent == "smart_eg"
        assert messages[-1]["role"] == "user"
        return {"tool_calls": [_tool_call_dict()]}

    client.set_stub(stub)
    result = asyncio.run(
        client.complete_with_tools(
            agent="smart_eg",
            messages=[{"role": "user", "content": "inspect db"}],
            tools=[_tool_schema()],
            tool_choice=tool_choice,
            stream=True,
            first_token_timeout_s=4.5,
        )
    )
    log.close()

    transcript_md = (log.run_dir / result.transcript_ref).read_text(encoding="utf-8")
    diagnostics = json.loads((log.run_dir / result.diagnostics_ref).read_text(encoding="utf-8"))

    assert diagnostics["tools"] == [_tool_schema()]
    assert diagnostics["tool_choice"] == tool_choice
    assert diagnostics["tool_calls"] == [_tool_call_dict()]
    assert "## Request Configuration" in transcript_md
    assert "| Temperature |" in transcript_md
    assert "| Max Tokens |" in transcript_md
    assert "| Tool Count | 1 |" in transcript_md
    assert "| Stream | True |" in transcript_md
    assert "| First Token Timeout (s) | 4.5 |" in transcript_md
    assert "## Tools" in transcript_md
    assert '"name": "list_collections"' in transcript_md
    assert "## Tool Choice" in transcript_md
    assert "## Tool Calls" in transcript_md
    assert "### list_collections (`call_1`)" in transcript_md
    assert '"db_id": "financial"' in transcript_md


def test_complete_with_tools_uses_streaming_with_first_token_timeout_metadata(tmp_path) -> None:
    settings = _settings(tmp_path, stub=False)
    log = setup_logging(settings.run_dir, console=False)
    client = LLMClient(settings, log)
    chunks = [
        _delta_chunk(
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id="call_1",
                    type="function",
                    function=SimpleNamespace(
                        name="list_collections",
                        arguments='{"db_id"',
                    ),
                )
            ]
        ),
        _delta_chunk(
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    function=SimpleNamespace(arguments=':"financial"}'),
                )
            ],
        ),
        _delta_chunk(
            finish_reason="tool_calls",
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        ),
    ]
    fake_openai = _FakeOpenAI(chunks)
    client._client = fake_openai
    tool_choice = {"type": "function", "function": {"name": "list_collections"}}

    result = asyncio.run(
        client.complete_with_tools(
            agent="smart_eg",
            messages=[{"role": "user", "content": "inspect db"}],
            tools=[_tool_schema()],
            tool_choice=tool_choice,
        )
    )
    log.close()

    kwargs = fake_openai.completions.calls[0]
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}
    assert kwargs["tools"] == [_tool_schema()]
    assert kwargs["tool_choice"] == tool_choice
    assert result.tool_calls == [
        ToolCall(
            id="call_1",
            name="list_collections",
            arguments={"db_id": "financial"},
            raw_arguments='{"db_id":"financial"}',
        )
    ]
    assert result.cost_source == "api"
    diagnostics = json.loads((log.run_dir / result.diagnostics_ref).read_text(encoding="utf-8"))
    assert diagnostics["stream"] is True
    assert diagnostics["first_token_timeout_s"] == 6.0
    assert diagnostics["cost_source"] == "api"


def test_validate_tool_message_pairs_rejects_unmatched_tool_result(tmp_path) -> None:
    settings = _settings(tmp_path)
    log = setup_logging(settings.run_dir, console=False)
    client = LLMClient(settings, log)

    with pytest.raises(Exception, match="tool message has no matching assistant tool_call"):
        asyncio.run(
            client.complete_with_tools(
                agent="smart_eg",
                messages=[
                    {"role": "user", "content": "inspect db"},
                    {
                        "role": "tool",
                        "tool_call_id": "missing",
                        "name": "list_collections",
                        "content": "{}",
                    },
                ],
                tools=[],
            )
        )


def test_validate_tool_message_pairs_requires_results_before_next_message(tmp_path) -> None:
    settings = _settings(tmp_path)
    log = setup_logging(settings.run_dir, console=False)
    client = LLMClient(settings, log)

    with pytest.raises(Exception, match="assistant tool_calls missing tool result messages"):
        asyncio.run(
            client.complete_with_tools(
                agent="smart_eg",
                messages=[
                    {"role": "user", "content": "inspect db"},
                    {"role": "assistant", "content": "", "tool_calls": [_tool_call_dict()]},
                    {"role": "user", "content": "continue"},
                ],
                tools=[_tool_schema()],
            )
        )


def test_complete_with_tools_retries_retryable_transport_errors(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, max_retries=1)
    log = setup_logging(settings.run_dir, console=False)
    client = LLMClient(settings, log)
    calls = 0

    async def raw_tool_call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RateLimitError("temporary rate limit")
        return _tool_response()

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(client, "_raw_tool_call", raw_tool_call)
    monkeypatch.setattr("tend.llm.client.asyncio.sleep", no_sleep)

    result = asyncio.run(
        client.complete_with_tools(
            agent="smart_eg",
            messages=[{"role": "user", "content": "inspect db"}],
            tools=[_tool_schema()],
        )
    )
    log.close()

    assert calls == 2
    assert result.attempts == 2
    assert result.tool_calls[0].name == "list_collections"
    assert any(event["event"] == "llm_transport_retry" for event in _read_events(log.run_dir))


def test_complete_with_tools_logs_tool_choice_fallback(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, max_retries=0)
    log = setup_logging(settings.run_dir, console=False)
    client = LLMClient(settings, log)
    tool_choice = {"type": "function", "function": {"name": "list_collections"}}
    seen_tool_choices = []

    async def raw_tool_call(
        _agent,
        _model,
        _messages,
        _temperature,
        _max_tokens,
        _tools,
        choice,
        _stream,
        _first_token_timeout_s,
    ):
        seen_tool_choices.append(choice)
        if choice is not None:
            raise LLMError(
                "provider rejected tool_choice",
                context={"status_code": 400, "field": "tool_choice"},
            )
        return _tool_response()

    monkeypatch.setattr(client, "_raw_tool_call", raw_tool_call)

    result = asyncio.run(
        client.complete_with_tools(
            agent="smart_eg",
            messages=[{"role": "user", "content": "inspect db"}],
            tools=[_tool_schema()],
            tool_choice=tool_choice,
        )
    )
    log.close()

    assert seen_tool_choices == [tool_choice, None]
    assert result.tool_choice_fallback is True
    fallback_events = [
        event for event in _read_events(log.run_dir)
        if event["event"] == "llm_tool_choice_fallback"
    ]
    assert fallback_events
    assert fallback_events[0]["requested_tool_choice"] == tool_choice
