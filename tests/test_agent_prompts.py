from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tend.agents import LLMAgent
from tend.config import Settings
from tend.solver.eg.runtime import SYSTEM_PROMPT, _tool_schemas


@pytest.fixture(scope="module")
def stub_settings() -> Settings:
    return Settings.from_env(overrides={"TEND_LLM_STUB": "1"}, run_id="pytest")


def test_llm_agent_prompt_text_reads_template_source_without_rewriting(tmp_path: Path) -> None:
    class PromptProbeAgent(LLMAgent):
        id = "prompt_probe"
        phase = "B"
        title = "Prompt Probe"
        prompt_file = "prompt_probe.md"

    raw_prompt = (
        "This sentence intentionally stays on one source line.\n"
        "\n"
        "- Preserve this bullet line\n"
        "  with its intended continuation.\n"
        "\n"
        "```json\n"
        "{\n"
        "  \"field\": 1\n"
        "}\n"
        "```\n"
    )
    prompt_path = tmp_path / "prompt_probe.md"
    prompt_path.write_text(raw_prompt, encoding="utf-8")
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            paths=SimpleNamespace(agent_prompts=tmp_path),
        ),
    )
    LLMAgent._prompt_cache.clear()

    text = PromptProbeAgent().prompt_text(ctx)

    assert text == raw_prompt


def test_agent_prompt_templates_are_english_and_not_hard_wrapped(
    stub_settings: Settings,
) -> None:
    cjk = re.compile(r"[\u2e80-\u9fff\u3000-\u30ff\uff00-\uffef]")
    fence = re.compile(r"^\s*(```|~~~)")
    heading = re.compile(r"^\s{0,3}#{1,6}\s")
    horizontal_rule = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
    table = re.compile(r"^\s*\|")
    list_item = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
    blockquote = re.compile(r"^\s*>")
    placeholder = re.compile(r"^\s*\{\{[^}]+\}\s*$")

    def is_plain_prose(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        if any(
            pattern.match(line)
            for pattern in (
                heading,
                horizontal_rule,
                table,
                list_item,
                blockquote,
                placeholder,
            )
        ):
            return False
        if line[:1].isspace():
            return False
        return stripped[:1] not in {"{", "}", "[", "]"}

    failures: list[str] = []
    for path in sorted(stub_settings.paths.agent_prompts.glob("*.md")):
        in_fence = False
        previous_plain: tuple[int, str] | None = None
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if fence.match(line):
                in_fence = not in_fence
                previous_plain = None
                continue
            if in_fence:
                continue
            if cjk.search(line):
                failures.append(f"{path.name}:{line_no}: contains CJK prompt text")
            if is_plain_prose(line):
                if previous_plain:
                    failures.append(
                        f"{path.name}:{previous_plain[0]}-{line_no}: prose hard wrap"
                    )
                previous_plain = (line_no, line)
            else:
                previous_plain = None

    assert failures == []


def test_agent_prompt_directory_marks_runtime_and_inactive_smart_templates(
    stub_settings: Settings,
) -> None:
    runtime_prompts = {
        "native_migration_designer.md",
        "native_nl_generator.md",
    }
    inactive_smart_templates = {
        "smart_intent_formalizer.md",
        "smart_nosql_planner.md",
    }
    prompt_files = {
        path.name
        for path in stub_settings.paths.agent_prompts.glob("*.md")
    }

    assert prompt_files == runtime_prompts | inactive_smart_templates


def test_smart_eg_live_system_prompt_teaches_batch2_runtime_contract() -> None:
    prompt = re.sub(r"\s+", " ", SYSTEM_PROMPT.lower().replace("`", ""))

    for phrase in [
        "stage order is mandatory",
        "only call tools exposed in the current turn",
        "accepted environment",
        "accepted intent",
        "accepted query_plan",
        "evidence_refs",
        "typed evidence",
        "value grounding",
        "relationship probe",
        "prefix tools",
        "tool_unimplemented",
        "final sanity execution",
        "production success",
        "typed feedback",
        "mongodb aggregation idioms",
        "$lookup only after proving relationship keys",
        "objectid",
        "isodate",
    ]:
        assert phrase in prompt

    assert "read_documents" in prompt
    assert "run_query" in prompt
    assert "not available" in prompt


def test_smart_eg_runtime_tool_schemas_describe_live_batch2_contract() -> None:
    schemas = {tool["function"]["name"]: tool["function"] for tool in _tool_schemas()}

    assert set(schemas) == {
        "abandon_with_failure",
        "add_evidence_claim",
        "check_ast_filter",
        "check_prefix_checkpoint",
        "discover_paths",
        "execute_pipeline_prefix",
        "inspect_array_shape",
        "inspect_dynamic_keys",
        "inspect_evidence_debt",
        "inspect_evidence_ledger",
        "link_evidence",
        "list_collections",
        "mine_counterexamples",
        "profile_path",
        "profile_path_values",
        "profile_relationship_candidates",
        "render_pipeline",
        "render_pipeline_prefix",
        "request_mode_shift",
        "request_revisit",
        "run_final_sanity_execution",
        "run_readonly_probe",
        "sample_documents",
        "search_values",
        "submit_environment_model",
        "submit_final_mql",
        "submit_intent_hypothesis",
        "submit_query_plan",
    }
    assert "read_documents" not in schemas
    assert "run_query" not in schemas

    def description(name: str) -> str:
        return str(schemas[name]["description"]).lower()

    assert "typed value grounding" in description("profile_path_values")
    assert "relationship probe" in description("profile_relationship_candidates")
    assert "if exposed" in description("profile_relationship_candidates")

    for name in [
        "render_pipeline_prefix",
        "execute_pipeline_prefix",
        "check_prefix_checkpoint",
    ]:
        assert "unimplemented" in description(name)
        assert "non-success" in description(name)

    assert "accepted environment" in description("submit_intent_hypothesis")
    assert "accepted intent" in description("submit_query_plan")
    assert "accepted environment, intent, and query_plan" in description(
        "submit_final_mql"
    )
    assert "final sanity execution" in description("submit_final_mql")
    assert "production success" in description("submit_final_mql")
    assert "typed failure" in description("abandon_with_failure")
    assert "typed feedback" in description("run_readonly_probe")
