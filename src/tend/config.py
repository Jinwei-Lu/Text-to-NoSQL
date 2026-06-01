"""Runtime configuration — single source for paths, LLM provider, and toggles.

Loads ``.env`` (no python-dotenv dependency; a tiny parser keeps the stack lean) and
exposes a frozen :class:`Settings`. Everything downstream takes a ``Settings`` rather
than reading ``os.environ`` directly, so runs are reproducible and testable.

Provider is OpenAI-compatible (DeepSeek by default, per .env). Model can be overridden
globally (``TEND_MODEL``) or per-agent (``AgentModels``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import ConfigError


def _find_repo_root(start: Path | None = None) -> Path:
    """Walk up from this file until we find the repo marker (pyproject.toml + proposals/)."""
    here = (start or Path(__file__).resolve()).parent
    for cand in [here, *here.parents]:
        if (cand / "pyproject.toml").exists() and (cand / "proposals").is_dir():
            return cand
    # fall back to two levels up from src/tend/
    return Path(__file__).resolve().parents[2]


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser: ``KEY=VALUE`` lines, ``#`` comments, optional quotes.

    Does not mutate ``os.environ`` — returns a dict so callers control precedence.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def _env(envmap: dict[str, str], key: str, default: str | None = None) -> str | None:
    """os.environ wins over .env file (so a shell export overrides .env)."""
    return os.environ.get(key, envmap.get(key, default))


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.0
    max_tokens: int = 8192
    timeout_s: float = 120.0
    max_retries: int = 4
    # Concurrency switch for live LLM calls, enforced by LLMClient's semaphore gate:
    # ``max_concurrency > 0`` bounds concurrent provider calls to that many;
    # ``max_concurrency <= 0`` runs fully UNBOUNDED (no limit). Default 16.
    max_concurrency: int = 16
    #: per-agent model overrides (agent_id -> model); empty = use ``model`` for all
    agent_models: dict[str, str] = field(default_factory=dict)

    def model_for(self, agent_id: str) -> str:
        return self.agent_models.get(agent_id, self.model)


@dataclass(frozen=True)
class Paths:
    repo_root: Path
    bird_root: Path           # minidev/MINIDEV
    proposals: Path
    agent_prompts: Path       # proposals/agent_prompts
    schemas: Path             # proposals/schemas
    runs: Path                # runs/   (run-scoped output: logs, artifacts)
    dataset_out: Path         # construction dataset output (mongodb_schema/, test.json, ...)

    def ensure(self) -> None:
        for p in (self.runs, self.dataset_out):
            p.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings
    paths: Paths
    mongo_uri: str
    mongo_db_prefix: str = "tend_"   # working dbs are <prefix><db_id>
    stub: bool = False               # offline: deterministic fake LLM, no live calls
    quiet: bool = False              # suppress the live progress UI (CI / logs only)
    seed: int = 0
    run_id: str = "dev"

    @classmethod
    def from_env(
        cls,
        *,
        run_id: str = "dev",
        overrides: dict[str, str] | None = None,
        require_bird: bool = True,
        require_llm: bool = True,
    ) -> "Settings":
        root = _find_repo_root()
        envmap = load_dotenv(root / ".env")
        if overrides:
            envmap = {**envmap, **overrides}

        api_key = _env(envmap, "OPENAI_API_KEY")
        base_url = _env(envmap, "OPENAI_BASE_URL")
        stub = _env(envmap, "TEND_LLM_STUB", "0") == "1"
        if require_llm and not stub:
            if not api_key or api_key.startswith("your-"):
                raise ConfigError(
                    "OPENAI_API_KEY is unset/placeholder; set it in .env or use TEND_LLM_STUB=1",
                    context={"env_file": str(root / ".env")},
                )
            if not base_url or base_url.startswith("your-"):
                raise ConfigError("OPENAI_BASE_URL is unset/placeholder", context={})

        bird_root = root / _env(envmap, "TEND_BIRD_ROOT", "minidev/MINIDEV")
        if require_bird and not bird_root.exists():
            raise ConfigError(
                f"BIRD mini-dev root not found at {bird_root}",
                context={"hint": "set TEND_BIRD_ROOT or place data under minidev/MINIDEV"},
            )

        llm = LLMSettings(
            base_url=base_url or "http://stub.invalid",
            api_key=api_key or "stub",
            model=_env(envmap, "TEND_MODEL", "deepseek-v4-flash") or "deepseek-v4-flash",
            temperature=float(_env(envmap, "TEND_TEMPERATURE", "0.0")),
            # reasoning models (deepseek-v4-flash) spend completion tokens on hidden
            # reasoning before the answer, so the budget must cover reasoning + output
            max_tokens=int(_env(envmap, "TEND_MAX_TOKENS", "16384")),
            timeout_s=float(_env(envmap, "TEND_TIMEOUT_S", "120")),
            max_retries=int(_env(envmap, "TEND_MAX_RETRIES", "4")),
            # TEND_LLM_MAX_CONCURRENCY bounds concurrent live LLM calls (default 16);
            # 0 (or any value <= 0) runs fully unbounded.
            max_concurrency=int(_env(envmap, "TEND_LLM_MAX_CONCURRENCY", "16")),
        )
        paths = Paths(
            repo_root=root,
            bird_root=bird_root,
            proposals=root / "proposals",
            agent_prompts=root / "proposals" / "agent_prompts",
            schemas=root / "proposals" / "schemas",
            runs=root / "runs",
            dataset_out=root / (
                _env(envmap, "TEND_DATASET_OUT")
                or f"runs/{run_id}/dataset"
            ),
        )
        return cls(
            llm=llm,
            paths=paths,
            mongo_uri=_env(envmap, "TEND_MONGO_URI", "mongodb://localhost:27017") or "",
            stub=stub,
            quiet=_env(envmap, "TEND_QUIET", "0") == "1",
            seed=int(_env(envmap, "TEND_SEED", "0")),
            run_id=run_id,
        )

    def with_run_id(self, run_id: str) -> "Settings":
        return replace(self, run_id=run_id)

    @property
    def run_dir(self) -> Path:
        return self.paths.runs / self.run_id
