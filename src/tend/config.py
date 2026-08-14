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
from .run_ids import new_run_id


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


def _compose_env(
    dotenv_values: dict[str, str],
    overrides: dict[str, str] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Compose config layers as .env < os.environ < explicit CLI overrides."""
    envmap = dict(dotenv_values)
    sources = {key: "envmap" for key in envmap}
    for key, value in os.environ.items():
        envmap[key] = value
        sources[key] = "environment"
    for key, value in (overrides or {}).items():
        envmap[key] = value
        sources[key] = "override"
    return envmap, sources


def _env(envmap: dict[str, str], key: str, default: str | None = None) -> str | None:
    return envmap.get(key, default)


def _env_with_source(
    envmap: dict[str, str],
    sources: dict[str, str],
    key: str,
    default: str | None = None,
) -> tuple[str | None, str]:
    if key in envmap:
        return envmap[key], sources.get(key, "envmap")
    return default, "default"


def _config_parse_error(
    key: str,
    raw_value: str | None,
    source: str,
    default: str | None,
    expected: str,
) -> ConfigError:
    return ConfigError(
        f"{key} must be {expected}; got {raw_value!r}",
        context={
            "key": key,
            "raw_value": raw_value,
            "source": source,
            "default": default,
        },
    )


def _env_bool(
    envmap: dict[str, str],
    sources: dict[str, str],
    key: str,
    default: str = "0",
) -> bool:
    raw, source = _env_with_source(envmap, sources, key, default)
    text = str(raw).strip()
    if text == "1":
        return True
    if text == "0":
        return False
    raise _config_parse_error(key, raw, source, default, "'1' or '0'")


def _env_int(envmap: dict[str, str], sources: dict[str, str], key: str, default: str) -> int:
    raw, source = _env_with_source(envmap, sources, key, default)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise _config_parse_error(key, raw, source, default, "an integer") from exc


def _env_float(
    envmap: dict[str, str],
    sources: dict[str, str],
    key: str,
    default: str,
) -> float:
    raw, source = _env_with_source(envmap, sources, key, default)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise _config_parse_error(key, raw, source, default, "a float") from exc


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.0
    # Some reasoning endpoints reject ``temperature`` rather than ignoring it.
    # Keep the historical default request unchanged and let an experiment runner
    # explicitly omit the field for those models.
    omit_temperature: bool = False
    max_tokens: int = 8192
    # Omit the provider max-token field for every completion. This lets a campaign
    # match the published reasoning-method request shape across direct and agentic arms.
    omit_max_tokens: bool = False
    # Rebuttal sensitivity runs can impose one explicit per-call output budget across
    # methods even when a method historically requested ``omit_max_tokens=True``.
    # Default False preserves the published/native method request shape.
    force_max_tokens: bool = False
    timeout_s: float = 120.0
    # Provider/transport faults (rate-limit, timeout, connection, empty/truncated) are
    # retried forever when ``max_retries < 0`` (the default): the provider is the only
    # thing that can recover, so we wait it out at ``retry_interval_s`` rather than giving
    # up. ``max_retries >= 0`` caps the attempts (used by tests for fast, bounded failure).
    max_retries: int = -1
    # A provider-native ``finish_reason=length`` is NOT a transient transport fault: at
    # temperature 0 the identical request tends to reproduce it, and each occurrence has
    # already burned the provider's full completion budget (observed: 65,536 reasoning-only
    # tokens, zero content). Re-issuing it under ``max_retries`` turns recovery into a cost
    # amplifier. Truncation therefore gets its OWN small budget, counted per logical call
    # and never shared with the transport budget. Measured on a real 30-record shard:
    # budget 0 -> 26/30 records keep a usable candidate; budget 1 -> 29/30 (same coverage
    # as the old unbounded behaviour) at 44% of the spend. Hence the default of 1.
    max_truncation_retries: int = 1
    # Fixed delay between provider-fault retries (seconds). Output-quality faults
    # (JSON parse / schema) use the separate bounded json-repair loop, not this.
    retry_interval_s: float = 5.0
    # Stream responses by default and treat the first streamed token as the provider
    # health signal: if it does not arrive within ``first_token_timeout_s`` the provider
    # is considered stalled and the call is retried (forever, per ``max_retries``).
    stream: bool = True
    first_token_timeout_s: float = 10.0
    # Concurrency switch for live LLM calls, enforced by LLMClient's semaphore gate:
    # ``max_concurrency > 0`` bounds concurrent provider calls to that many;
    # ``max_concurrency <= 0`` runs fully UNBOUNDED (no limit). Default 0 (unbounded) —
    # the provider sustains thousands of parallel calls, so we don't throttle here.
    max_concurrency: int = 0
    # Observability threshold. ``slow_call_warn_s`` emits a warning after a slow LLM call
    # completes. ``<= 0`` disables.
    slow_call_warn_s: float = 45.0
    reasoning_effort: str | None = None
    thinking: str | None = None
    # Optional OpenRouter routing controls.  They are inert unless explicitly enabled
    # by an experiment runner, so native OpenAI/DeepSeek clients retain their existing
    # request shape.  Pinning a provider is important for repeatability studies: without
    # it, router-level endpoint changes are confounded with model nondeterminism.
    openrouter_provider_only: tuple[str, ...] = ()
    openrouter_allow_fallbacks: bool = False
    openrouter_require_parameters: bool = True
    openrouter_metadata: bool = False
    # DynaDB-style per-call markdown transcripts are the default human log surface.
    # Set TEND_LLM_TRANSCRIPT_MD=0 only for diagnostics-JSON-only CI runs.
    write_markdown_transcripts: bool = True
    #: per-agent model overrides (agent_id -> model); empty = use ``model`` for all
    agent_models: dict[str, str] = field(default_factory=dict)

    def model_for(self, agent_id: str) -> str:
        return self.agent_models.get(agent_id, self.model)


@dataclass(frozen=True)
class Paths:
    repo_root: Path
    bird_root: Path  # minidev/MINIDEV
    proposals: Path
    agent_prompts: Path  # proposals/agent_prompts
    schemas: Path  # proposals/schemas
    runs: Path  # runs/   (run-scoped output: logs, artifacts)
    dataset_out: Path  # construction dataset output (mongodb_schema/, test.json, ...)

    def ensure(self) -> None:
        for p in (self.runs, self.dataset_out):
            p.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings
    paths: Paths
    mongo_uri: str
    mongo_db_prefix: str = "tend_"  # working dbs are <prefix><db_id>
    use_existing_mongo_dbs: bool = False
    # High-concurrency suite runs push hundreds of concurrent Mongo aggregations through
    # asyncio.to_thread; pymongo's default pool (100) and asyncio's default thread pool
    # (min(32, cpus+4)) both silently serialize them. Sized for --workers 440 suites.
    mongo_max_pool_size: int = 200
    to_thread_workers: int = 128
    stub: bool = False  # offline: deterministic fake LLM, no live calls
    quiet: bool = False  # suppress the live progress UI (CI / logs only)
    seed: int = 0
    run_id: str = field(default_factory=new_run_id)

    @classmethod
    def from_env(
        cls,
        *,
        run_id: str | None = None,
        overrides: dict[str, str] | None = None,
        require_bird: bool = True,
        require_llm: bool = True,
    ) -> "Settings":
        run_id = run_id or new_run_id()
        root = _find_repo_root()
        envmap, sources = _compose_env(load_dotenv(root / ".env"), overrides)

        api_key = _env(envmap, "OPENAI_API_KEY")
        base_url = _env(envmap, "OPENAI_BASE_URL")
        stub = _env_bool(envmap, sources, "TEND_LLM_STUB", "0")
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
            temperature=_env_float(envmap, sources, "TEND_TEMPERATURE", "0.0"),
            omit_temperature=_env_bool(envmap, sources, "TEND_OMIT_TEMPERATURE", "0"),
            # reasoning models (deepseek-v4-flash) spend completion tokens on hidden
            # reasoning before the answer, so the budget must cover reasoning + output
            max_tokens=_env_int(envmap, sources, "TEND_MAX_TOKENS", "16384"),
            omit_max_tokens=_env_bool(envmap, sources, "TEND_OMIT_MAX_TOKENS", "0"),
            force_max_tokens=_env_bool(envmap, sources, "TEND_FORCE_MAX_TOKENS", "0"),
            timeout_s=_env_float(envmap, sources, "TEND_TIMEOUT_S", "120"),
            # TEND_MAX_RETRIES < 0 (default) retries provider faults forever; >= 0 caps them.
            max_retries=_env_int(envmap, sources, "TEND_MAX_RETRIES", "-1"),
            max_truncation_retries=_env_int(
                envmap, sources, "TEND_MAX_TRUNCATION_RETRIES", "1"
            ),
            # TEND_LLM_RETRY_INTERVAL_S is the fixed wait between provider-fault retries.
            retry_interval_s=_env_float(envmap, sources, "TEND_LLM_RETRY_INTERVAL_S", "5"),
            # TEND_LLM_STREAM toggles streaming; TEND_LLM_FIRST_TOKEN_TIMEOUT_S sets the
            # first-token (provider-health) deadline that, when missed, triggers a retry.
            stream=_env_bool(envmap, sources, "TEND_LLM_STREAM", "1"),
            first_token_timeout_s=_env_float(
                envmap, sources, "TEND_LLM_FIRST_TOKEN_TIMEOUT_S", "10"
            ),
            # TEND_LLM_MAX_CONCURRENCY bounds concurrent live LLM calls (default 0 =
            # unbounded); any value <= 0 runs fully unbounded.
            max_concurrency=_env_int(envmap, sources, "TEND_LLM_MAX_CONCURRENCY", "0"),
            slow_call_warn_s=_env_float(envmap, sources, "TEND_LLM_SLOW_WARN_S", "45"),
            reasoning_effort=(
                _env(envmap, "TEND_REASONING_EFFORT") or _env(envmap, "TEND_LLM_REASONING_EFFORT")
            ),
            thinking=(_env(envmap, "TEND_THINKING") or _env(envmap, "TEND_LLM_THINKING")),
            openrouter_provider_only=tuple(
                item.strip()
                for item in (_env(envmap, "TEND_OPENROUTER_PROVIDER_ONLY") or "").split(",")
                if item.strip()
            ),
            openrouter_allow_fallbacks=_env_bool(
                envmap, sources, "TEND_OPENROUTER_ALLOW_FALLBACKS", "0"
            ),
            openrouter_require_parameters=_env_bool(
                envmap, sources, "TEND_OPENROUTER_REQUIRE_PARAMETERS", "1"
            ),
            openrouter_metadata=_env_bool(envmap, sources, "TEND_OPENROUTER_METADATA", "0"),
            write_markdown_transcripts=_env_bool(envmap, sources, "TEND_LLM_TRANSCRIPT_MD", "1"),
        )
        paths = Paths(
            repo_root=root,
            bird_root=bird_root,
            proposals=root / "proposals",
            agent_prompts=root / "proposals" / "agent_prompts",
            schemas=root / "proposals" / "schemas",
            runs=root / "runs",
            dataset_out=root / (_env(envmap, "TEND_DATASET_OUT") or f"runs/{run_id}/dataset"),
        )
        return cls(
            llm=llm,
            paths=paths,
            mongo_uri=_env(envmap, "TEND_MONGO_URI", "mongodb://localhost:27017") or "",
            use_existing_mongo_dbs=_env_bool(envmap, sources, "TEND_USE_EXISTING_MONGO_DBS", "0"),
            mongo_max_pool_size=_env_int(envmap, sources, "TEND_MONGO_MAX_POOL_SIZE", "200"),
            to_thread_workers=_env_int(envmap, sources, "TEND_TO_THREAD_WORKERS", "128"),
            stub=stub,
            quiet=_env_bool(envmap, sources, "TEND_QUIET", "0"),
            seed=_env_int(envmap, sources, "TEND_SEED", "0"),
            run_id=run_id,
        )

    def with_run_id(self, run_id: str) -> "Settings":
        return replace(self, run_id=run_id)

    @property
    def run_dir(self) -> Path:
        return self.paths.runs / self.run_id
