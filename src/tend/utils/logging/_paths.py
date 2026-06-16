from __future__ import annotations

# ruff: noqa: F403,F405

from tend.utils.logging._config import *

def _timestamp_suffix() -> str:
    now_ns = time.time_ns()
    dt = datetime.fromtimestamp(now_ns // 1_000_000_000, tz=timezone.utc)
    return f"{dt:%Y%m%d_%H%M%S}_{now_ns % 1_000_000_000:09d}"

def _normalize_iter_label(label: str = "") -> str:
    """Return the unified ``iter_NN_step`` prefix used by LLM log files."""

    safe_label = safe_dirname(label).lower()
    if not safe_label or safe_label == "_":
        return "iter_00_llm"
    if safe_label == "seed" or safe_label.startswith("seed_"):
        return safe_label

    iter_match = _ITER_LABEL_RE.match(safe_label)
    if iter_match:
        iter_num = int(iter_match.group("iter"))
        step = (iter_match.group("step") or "llm").strip("_") or "llm"
        return f"iter_{iter_num:02d}_{step}"

    legacy_match = _LEGACY_ITER_LABEL_RE.match(safe_label)
    if legacy_match:
        iter_num = int(legacy_match.group("iter"))
        step = legacy_match.group("step")
        tail = legacy_match.group("tail").strip("_")
        step_label = "_".join(part for part in (step, tail) if part)
        return f"iter_{iter_num:02d}_{step_label}"

    return f"iter_00_{safe_label.strip('_') or 'llm'}"

def _generate_call_id(label: str = "") -> str:
    return f"{_normalize_iter_label(label)}_{_timestamp_suffix()}"

def _open_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    raw = str(path.resolve())
    if raw.startswith("\\\\?\\"):
        return raw
    return "\\\\?\\" + raw

def _safe_task_log_path(stage_dir: Path, task_id: str) -> Path:
    """Return a filesystem-safe log path while preserving slash nesting."""

    parts = [safe_dirname(p) for p in re.split(r"[\\/]+", task_id) if p]
    if not parts:
        parts = ["_"]
    return stage_dir.joinpath(*parts[:-1], f"{parts[-1]}.log")

__all__ = [
    "_timestamp_suffix",
    "_normalize_iter_label",
    "_generate_call_id",
    "_open_path",
    "_safe_task_log_path",
]
