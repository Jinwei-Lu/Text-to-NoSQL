from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json
import subprocess
from urllib.parse import urlparse

from openai import OpenAI

DEFAULT_API_KEY = "sk-Zxj8saA9P2aXOfeZ6XOHTSqm3RJak9xXuRAzW6hIeVD04yz1"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
SUPPORTED_LLM_MODELS = (
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4.1-mini",
    "gpt-4.1",
)
DEFAULT_LLM_MODEL = "gpt-4o-mini"

class ExternalModelRunner:
    def generate(self, task_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class NoopExternalModelRunner(ExternalModelRunner):
    def generate(self, task_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"task_name": task_name, "candidates": [], "raw": None}


@dataclass
class CommandExternalModelRunner(ExternalModelRunner):
    command: str

    def generate(self, task_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        process = subprocess.run(
            self.command,
            input=json.dumps({"task_name": task_name, "payload": payload}, ensure_ascii=False),
            capture_output=True,
            text=True,
            shell=True,
            check=True,
        )
        response = json.loads(process.stdout.strip() or "{}")
        if "candidates" not in response:
            response["candidates"] = []
        return response


@dataclass
class OpenAICompatibleExternalModelRunner(ExternalModelRunner):
    base_url: str
    model: str
    api_key: str

    def __post_init__(self) -> None:
        self.base_url = normalize_openai_base_url(self.base_url)
        self.model = resolve_llm_model(self.model)
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def generate(self, task_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = payload.get("prompt") or json.dumps(payload, ensure_ascii=False, indent=2)
        completion = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a structured generation service for TEND construction. "
                        "Return strict JSON with a top-level 'candidates' array."
                    ),
                },
                {
                    "role": "user",
                    "content": f"task={task_name}\n{prompt}",
                },
            ],
            response_format={"type": "json_object"},
        )
        content = completion.choices[0].message.content or "{}"
        parsed = json.loads(content)
        if "candidates" not in parsed:
            parsed["candidates"] = []
        return parsed


def build_external_model_runner(
    runner_kind: str = "noop",
    command: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    api_key: str | None = None,
) -> ExternalModelRunner:
    if runner_kind == "noop":
        return NoopExternalModelRunner()
    if runner_kind == "command":
        if not command:
            raise ValueError("command runner requires a command string")
        return CommandExternalModelRunner(command=command)
    if runner_kind == "openai-compatible":
        resolved_api_key = api_key or DEFAULT_API_KEY
        if not base_url:
            raise ValueError("openai-compatible runner requires base_url")
        return OpenAICompatibleExternalModelRunner(
            base_url=base_url,
            model=resolve_llm_model(model),
            api_key=resolved_api_key,
        )
    raise ValueError(f"Unknown external runner kind: {runner_kind}")


def normalize_openai_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        path = f"{path}/v1" if path else "/v1"
    return parsed._replace(path=path).geturl()


def resolve_llm_model(model: str | None) -> str:
    resolved = model or DEFAULT_LLM_MODEL
    if resolved not in SUPPORTED_LLM_MODELS:
        raise ValueError(
            f"Unsupported llm model: {resolved}. Supported models: {', '.join(SUPPORTED_LLM_MODELS)}"
        )
    return resolved
