from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tend.agents import LLMAgent
from tend.config import Settings


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


def test_active_prompt_directory_contains_only_runtime_prompts(
    stub_settings: Settings,
) -> None:
    assert {
        path.name
        for path in stub_settings.paths.agent_prompts.glob("*.md")
    } == {
        "native_migration_designer.md",
        "native_nl_generator.md",
        "smart_intent_formalizer.md",
        "smart_nosql_planner.md",
    }
