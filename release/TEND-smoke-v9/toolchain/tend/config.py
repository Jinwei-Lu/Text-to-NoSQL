"""Unified LLM/runtime configuration entry point."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import threading
import yaml
from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parents[1]
PROPOSALS_ROOT = REPO_ROOT / "proposals"
SCHEMAS_ROOT = PROPOSALS_ROOT / "schemas"
PROMPTS_ROOT = PROPOSALS_ROOT / "agent_prompts"
FIXTURES_ROOT = PROPOSALS_ROOT / "fixtures"
SPIDER_DATA_ROOT = PROPOSALS_ROOT / "spider_data"


def _load_dotenv() -> None:
    env_path = REPO_ROOT / "infra" / "env" / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv()

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

LLM_DEFAULTS = {
    "temperature": 0.0,
    "top_p": 1.0,
    "seed": 0xC0FFEE,
    "timeout": 120,
    "max_retries": 2,
}

# Max in-flight live API calls per process (ThreadPoolExecutor workers may exceed this).
LLM_MAX_CONCURRENCY = max(1, int(os.getenv("TEND_LLM_MAX_CONCURRENCY", "128")))

_CLIENT: OpenAI | None = None
_CLIENT_LOCK = threading.Lock()

LLM_CACHE_DIR = Path(os.getenv("TEND_LLM_CACHE", "out/.llm_cache"))
RUN_DIR = Path(os.getenv("TEND_RUN_DIR", "out/runs")) / datetime.now(timezone.utc).strftime(
    "%Y%m%dT%H%M%SZ"
)

MONGO_URI = os.getenv("TEND_MONGO_URI", "mongodb://localhost:27017")
MONGO_IMAGE = "mongodb/mongodb-community-server:7.0.14-ubuntu2204"


def force_document_flex() -> bool:
    """When True (default), apply H0 build-policy polymorphic flex for qualifying dbs."""
    return os.getenv("TEND_FORCE_DOCUMENT_FLEX", "1") != "0"


def pool_disjoint_strict() -> bool:
    """When False (default), allow the same model id across multiple LLM pools."""
    return os.getenv("TEND_POOL_DISJOINT", "0") == "1"


def llm_enabled() -> bool:
    """True when an API key is configured and stub mode is not forced."""
    if os.getenv("TEND_LLM_STUB", "0") == "1":
        return False
    return bool(OPENAI_API_KEY)


def default_llm_stub() -> bool:
    return not llm_enabled()


def use_fixtures() -> bool:
    """Fixture YAML/JSON shortcuts — disabled when real LLM is active."""
    return default_llm_stub()


def assert_pilot_llm_live() -> None:
    """Raise RuntimeError if Pilot-B is running in stub/fixture mode."""
    if os.getenv("TEND_LLM_STUB", "0") == "1":
        raise RuntimeError("Pilot-B requires TEND_LLM_STUB≠1 (real LLM API)")
    if not llm_enabled():
        raise RuntimeError("Pilot-B requires OPENAI_API_KEY and llm_enabled()=True")
    if use_fixtures():
        raise RuntimeError("Pilot-B requires use_fixtures()=False (no fixture YAML shortcuts)")


def assert_gate_f_no_stubs(*, panel_stub: bool, llm_stub: bool | None = None) -> None:
    """Gate-F release checks for stub flags."""
    if panel_stub:
        raise RuntimeError("Gate-F requires panel_stub=false")
    if llm_stub is None:
        llm_stub = default_llm_stub()
    if llm_stub:
        raise RuntimeError("Gate-F requires llm_stub=false")


def make_client() -> OpenAI:
    global _CLIENT
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to tend/config.py, infra/env/.env, or your shell."
        )
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        import httpx

        limits = httpx.Limits(
            max_connections=LLM_MAX_CONCURRENCY,
            max_keepalive_connections=LLM_MAX_CONCURRENCY,
        )
        _CLIENT = OpenAI(
            base_url=OPENAI_BASE_URL,
            api_key=OPENAI_API_KEY,
            timeout=LLM_DEFAULTS["timeout"],
            max_retries=LLM_DEFAULTS["max_retries"],
            http_client=httpx.Client(limits=limits),
        )
        return _CLIENT


def load_pool_roster() -> dict:
    path = Path(__file__).parent / "core" / "llm_pools.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


POOL_ROSTER: dict = load_pool_roster()
