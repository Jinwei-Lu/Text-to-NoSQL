"""DynaDB-style live terminal progress for TEND runs.

The terminal surface is intentionally modeled after DynaDB's progress panel:
top-level stages, one current phase bar, one activity line, LLM retry/provider
wait banners, a bounded subtask table, an error block, and stage/total usage
footers. The logger remains the durable source for run artifacts; this module
only owns the live panel and the root ``progress.jsonl`` event stream.
"""
from __future__ import annotations

import functools
import json
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ._runtime import RunLogger


class TaskStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


_STATUS_ICONS: dict[TaskStatus, str] = {
    TaskStatus.PENDING: "[dim]◌[/]",
    TaskStatus.QUEUED: "[dim yellow]◔[/]",
    TaskStatus.RUNNING: "[yellow]⟳[/]",
    TaskStatus.COMPLETED: "[green]✓[/]",
    TaskStatus.FAILED: "[red]✗[/]",
    TaskStatus.SKIPPED: "[dim]↷[/]",
    TaskStatus.CANCELLED: "[yellow]⊘[/]",
}

_STAGE_STATUS_ICONS: dict[StageStatus, str] = {
    StageStatus.PENDING: "[dim]○[/]",
    StageStatus.RUNNING: "[yellow]●[/]",
    StageStatus.COMPLETED: "[green]●[/]",
    StageStatus.SKIPPED: "[dim]↷[/]",
    StageStatus.FAILED: "[red]●[/]",
    StageStatus.CANCELLED: "[yellow]⊘[/]",
}

_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_HEARTBEAT_INTERVAL_S = 0.5
_LLM_RETRY_STATUS_TTL_S = 15.0
_LLM_PROVIDER_STATUS_TTL_S = 90.0
_LLM_BOUNDED_RETRY_CAPS: dict[str, int] = {
    "empty_content": 5,
    "structured_parse": 2,
    "structured_json": 2,
    "tool_parse": 2,
}
_LLM_PROVIDER_RETRY_REASONS = {"network_transient", "rate_limit"}

_PROGRESS_ENV = "TEND_PROGRESS"
_PROGRESS_FALLBACK_ENV = "DYNADB_PROGRESS"
_VALID_PROGRESS_MODES = {"auto", "rich", "plain", "off"}
_PLAIN_UPDATE_MIN_INTERVAL_S = 1.0
_TASK_DISPLAY_MODES = {"all", "current_batch", "active"}
_PROGRESS_EXPAND_ENV = "TEND_PROGRESS_EXPAND"
_PROGRESS_EXPAND_FALLBACK_ENV = "DYNADB_PROGRESS_EXPAND"
_PHASE_BAR_MIN_WIDTH = 32
_PHASE_BAR_MAX_WIDTH = 72

_WATCH_EVENTS = {
    "agent_contract_retry",
    "agent_postprocess_retry",
    "branch_failed",
    "duplicate_mql_rejected",
    "llm_repair_retry",
    "llm_slow_call",
    "llm_transport_retry",
    "ms_gold_lock_retry",
    "pv_mutation_exec_fail",
    "pv_reject",
    "record_dropped",
    "rtv_reject",
    "sc_reject",
}


class _TaskState:
    __slots__ = (
        "task_id",
        "label",
        "status",
        "detail",
        "submitted",
        "parent_id",
        "group",
        "anomaly",
        "last_transition_at",
    )

    def __init__(
        self,
        task_id: str,
        label: str,
        parent_id: str = "",
        group: str = "",
    ) -> None:
        self.task_id = task_id
        self.label = label
        self.status = TaskStatus.PENDING
        self.detail = ""
        self.submitted = ""
        self.parent_id = parent_id
        self.group = group
        self.anomaly = ""
        self.last_transition_at = 0.0


class _PipelineStageState:
    __slots__ = (
        "stage_id",
        "label",
        "status",
        "detail",
        "last_transition_at",
    )

    def __init__(self, stage_id: str, label: str) -> None:
        self.stage_id = stage_id
        self.label = label
        self.status = StageStatus.PENDING
        self.detail = ""
        self.last_transition_at = 0.0


@dataclass
class _GroupState:
    label: str
    phase: str
    total: int | None = None
    order: int = 0


_FINISHED_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.SKIPPED,
    TaskStatus.CANCELLED,
}

_TASK_TREE_INDENT = "    "


def _split_task_tree(
    tasks: dict[str, _TaskState],
) -> tuple[list[_TaskState], dict[str, list[_TaskState]]]:
    root_tasks: list[_TaskState] = []
    child_tasks: dict[str, list[_TaskState]] = {}
    for task in tasks.values():
        if task.parent_id and task.parent_id in tasks:
            child_tasks.setdefault(task.parent_id, []).append(task)
        else:
            root_tasks.append(task)
    return root_tasks, child_tasks


def _visible_task_window(
    candidates: list[_TaskState],
    *,
    current_batch_only: bool,
    max_finished: int,
    max_pending: int,
) -> tuple[list[_TaskState], int, int]:
    running: list[_TaskState] = []
    finished: list[_TaskState] = []
    pending: list[_TaskState] = []
    for candidate in candidates:
        if candidate.status == TaskStatus.RUNNING:
            running.append(candidate)
        elif candidate.status in _FINISHED_TASK_STATUSES:
            finished.append(candidate)
        else:
            pending.append(candidate)

    visible_finished = finished[:max_finished]
    if current_batch_only and (running or finished):
        visible_pending: list[_TaskState] = []
    else:
        visible_pending = pending[:max_pending]
    return (
        running + visible_finished + visible_pending,
        len(finished) - len(visible_finished),
        len(pending) - len(visible_pending),
    )


def _add_task_table_row(
    table: Table,
    task: _TaskState,
    *,
    show_submitted: bool,
    depth: int = 0,
) -> None:
    icon = _STATUS_ICONS.get(task.status, "?")
    detail = f" {task.detail}" if task.detail else ""
    anomaly = f" [red]!{task.anomaly}[/]" if task.anomaly else ""
    status_cell = f"{icon} {task.status.value.capitalize()}{detail}{anomaly}"
    label_cell = f"{_TASK_TREE_INDENT * depth}{task.label}"
    if show_submitted:
        table.add_row(label_cell, status_cell, task.submitted)
    else:
        table.add_row(label_cell, status_cell)


def _add_task_summary_row(
    table: Table,
    *,
    hidden_finished: int,
    hidden_pending: int,
    display_mode: str,
    show_submitted: bool,
    depth: int = 0,
) -> None:
    if not hidden_finished and not hidden_pending:
        return
    summary_parts: list[str] = []
    if hidden_finished:
        summary_parts.append(f"[green]✓[/] {hidden_finished} more completed")
    if hidden_pending:
        if display_mode == "current_batch":
            pending_label = "queued outside current batch"
        elif display_mode == "active":
            pending_label = "pending"
        else:
            pending_label = "more pending"
        summary_parts.append(f"[dim]◌[/] {hidden_pending} {pending_label}")
    summary_cell = " · ".join(summary_parts)
    summary_label = f"{_TASK_TREE_INDENT * depth}[dim]…[/]"
    if show_submitted:
        table.add_row(summary_label, summary_cell, "")
    else:
        table.add_row(summary_label, summary_cell)


def _phase_bar_width(available_width: int) -> int:
    return min(
        _PHASE_BAR_MAX_WIDTH,
        max(_PHASE_BAR_MIN_WIDTH, (available_width - 24) // 2),
    )


def _format_progress_bar(completed: int, total: int, *, width: int) -> str:
    if total <= 0:
        return "╺" + ("─" * (width - 1))
    bounded = max(0, min(completed, total))
    if bounded >= total:
        return "━" * width
    filled = int(width * (bounded / total))
    if bounded > 0:
        filled = max(1, filled)
    if filled <= 0:
        return "╺" + ("─" * (width - 1))
    return ("━" * filled) + "╸" + ("─" * (width - filled - 1))


def _env_value(primary: str, fallback: str) -> str:
    raw = os.environ.get(primary)
    if raw is None:
        raw = os.environ.get(fallback, "")
    return raw.strip().lower()


def _resolve_progress_mode(console: Console) -> str:
    raw = _env_value(_PROGRESS_ENV, _PROGRESS_FALLBACK_ENV) or "auto"
    if raw not in _VALID_PROGRESS_MODES:
        raw = "auto"
    if raw in {"rich", "plain", "off"}:
        return raw
    if os.environ.get("CI") or not console.is_terminal:
        return "plain"
    return "rich"


def _plain_stage_line(stage: _PipelineStageState) -> str:
    detail = f" - {stage.detail}" if stage.detail else ""
    return f"Stage {stage.stage_id}: {stage.status.value} ({stage.label}){detail}"


def _plain_task_line(task: _TaskState) -> str:
    detail = f" - {task.detail}" if task.detail else ""
    submitted = f" [{task.submitted}]" if task.submitted else ""
    return f"Task {task.task_id}: {task.status.value}{detail}{submitted}"


def _fmt_duration(seconds: float) -> str:
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _coerce_task_status(status: str) -> TaskStatus:
    aliases = {
        "ok": TaskStatus.COMPLETED.value,
        "done": TaskStatus.COMPLETED.value,
        "success": TaskStatus.COMPLETED.value,
        "fail": TaskStatus.FAILED.value,
        "failure": TaskStatus.FAILED.value,
        "retry": TaskStatus.RUNNING.value,
    }
    raw = aliases.get(str(status).strip().lower(), str(status).strip().lower())
    try:
        return TaskStatus(raw)
    except ValueError:
        return TaskStatus.RUNNING


_F = TypeVar("_F", bound=Callable[..., Any])


def progress_safe(method_name: str) -> Callable[[_F], _F]:
    def decorator(func: _F) -> _F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - terminal UI must not kill a run
                self = args[0] if args else None
                log = getattr(self, "_log", None)
                if log is not None:
                    try:
                        log.warning(
                            f"progress_{method_name}_failed",
                            method=method_name,
                            error_type=type(exc).__name__,
                            message=str(exc),
                        )
                    except Exception:
                        pass
                return None

        return wrapper  # type: ignore[return-value]

    return decorator


class _ProgressEventSink:
    """Append progress JSONL until the sink fails, then latch degraded state."""

    def __init__(self, path: Path, logger: RunLogger) -> None:
        self.path = path
        self._log = logger
        self._lock = threading.Lock()
        self._degraded = False
        self._warning_emitted = False

    @property
    def degraded(self) -> bool:
        return self._degraded

    def write(self, payload: dict[str, Any], *, source_event: str) -> None:
        if self._degraded:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            with self._lock:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
        except Exception as exc:
            self._degraded = True
            if self._warning_emitted:
                return
            self._warning_emitted = True
            try:
                self._log.warning(
                    "progress_persist_failed",
                    source_event=source_event,
                    path=str(self.path),
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
            except Exception:
                pass


class ProgressReporter:
    """Terminal-only DynaDB-style progress display plus durable progress events."""

    MAX_VISIBLE_ERRORS = 5
    MAX_VISIBLE_FINISHED = 15
    MAX_VISIBLE_PENDING = 10

    def __init__(
        self,
        run_id: str,
        logger: RunLogger,
        *,
        enabled: bool = True,
        heartbeat_s: float = _HEARTBEAT_INTERVAL_S,
        stall_warn_s: float = 120.0,
    ) -> None:
        self.run_id = run_id
        self._run_id = run_id
        self._repo_name = "TEND"
        self._log = logger
        self._progress_path = logger.run_dir / "progress.jsonl"
        self._progress_sink = _ProgressEventSink(self._progress_path, logger)
        self._console = Console(stderr=True)
        self._mode = "off" if not enabled else _resolve_progress_mode(self._console)
        self._heartbeat_s = heartbeat_s
        self._stall_warn_s = stall_warn_s
        self._started = False
        self._plain_last_emit_at = 0.0
        self._plain_last_signature = ""
        self._lock = threading.RLock()

        expand = _env_value(_PROGRESS_EXPAND_ENV, _PROGRESS_EXPAND_FALLBACK_ENV)
        if expand in {"1", "true", "yes", "all"}:
            self.MAX_VISIBLE_FINISHED = 999
            self.MAX_VISIBLE_PENDING = 999
            self.MAX_VISIBLE_ERRORS = 999
        elif expand.isdigit() and int(expand) > 0:
            n = int(expand)
            self.MAX_VISIBLE_FINISHED = n
            self.MAX_VISIBLE_PENDING = n

        self._pipeline_group_name = "Pipeline stages"
        self._pipeline_stages: dict[str, _PipelineStageState] = {}
        self._pipeline_stage_order: list[str] = []

        self._phase_name = ""
        self._phase_id: str | None = None
        self._phase_current = 0
        self._phase_total = 0
        self._phase_detail = ""

        self._subtask_group_name = "Tasks"
        self._subtask_display_mode = "all"
        self._tasks: dict[str, _TaskState] = {}
        self._groups: dict[str, _GroupState] = {}
        self._archived_task_counts = {"started": 0, "ok": 0, "fail": 0}

        self._errors: deque[str] = deque(maxlen=self.MAX_VISIBLE_ERRORS)
        self._anoms: deque[dict[str, Any]] = deque(maxlen=8)
        self._alerts: deque[dict[str, Any]] = deque(maxlen=12)
        self._anom_by_kind: dict[str, int] = {}
        self._alert_by_event: dict[str, int] = {}
        self._counts = {"started": 0, "ok": 0, "fail": 0, "retry": 0}

        self._activity = ""
        self._spinner_tick = 0
        self._llm_retry_status = ""
        self._llm_retry_updated_at = 0.0
        self._llm_retry_attempt = 0
        self._llm_retry_reason = ""
        self._llm_retry_next_wait_s = 0.0
        self._llm_retry_visible_until_at = 0.0
        self._llm_retry_started_at = 0.0
        self._llm_provider_status = ""
        self._llm_provider_updated_at = 0.0
        self._llm_provider_started_at = 0.0
        self._llm_provider_visible_until_at = 0.0
        self._last_llm_call_time = 0.0

        self._cost_usd = 0.0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cache_hit_tokens = 0
        self._cache_miss_tokens = 0
        self._llm_calls = 0
        self._start_time = time.time()
        self._total_cost_usd = 0.0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_cache_hit_tokens = 0
        self._total_cache_miss_tokens = 0
        self._total_llm_calls = 0
        self._pipeline_start_time = time.time()
        self._cost_sources_seen: set[str] = set()

        self._live: Live | None = None
        self._heartbeat_stop: threading.Event | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._last_activity = time.monotonic()

        logger.subscribe_anomaly(self._on_anomaly)
        logger.subscribe_event(self._on_event)
        self._record_progress_event("progress_init", source="tend_dynadb_progress")

    @progress_safe("start")
    def start(self) -> None:
        self._started = True
        self._record_progress_event("progress_start", mode=self._mode)
        if self._mode == "off":
            return
        if self._mode == "plain":
            self._plain_emit(f"TEND progress started run={self._run_id}", force=True)
            self._start_heartbeat()
            return
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=4,
        )
        self._live.start()
        self._start_heartbeat()

    @progress_safe("stop")
    def stop(self) -> None:
        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.0)
            self._heartbeat_thread = None
        self._heartbeat_stop = None
        if self._live:
            self._live.stop()
            self._live = None
        self._started = False
        self._record_progress_event("progress_stop")

    def _start_heartbeat(self) -> None:
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"tend-progress-heartbeat-{self._run_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        assert self._heartbeat_stop is not None
        while not self._heartbeat_stop.wait(self._heartbeat_s):
            if self._mode == "rich" and self._live is None:
                break
            try:
                self._refresh()
            except Exception:
                try:
                    self._log.warning("progress_heartbeat_refresh_failed")
                except Exception:
                    pass

    @progress_safe("force_refresh")
    def force_refresh(self) -> None:
        live = self._live
        if live is None:
            return
        live.update(self._render())
        live.refresh()

    def __enter__(self) -> "ProgressReporter":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()

    @progress_safe("configure_pipeline")
    def configure_pipeline(
        self,
        stages: list[dict[str, str] | tuple[str, str] | str],
        *,
        group_name: str = "Pipeline stages",
    ) -> None:
        with self._lock:
            self._pipeline_group_name = group_name
            self._pipeline_stages = {}
            self._pipeline_stage_order = []
            for item in stages:
                if isinstance(item, str):
                    stage_id = item
                    label = item
                elif isinstance(item, tuple):
                    stage_id, label = item
                else:
                    stage_id = item.get("stage_id") or item.get("id") or item.get("key")
                    label = item.get("label") or item.get("name") or stage_id
                if not stage_id:
                    continue
                sid = str(stage_id)
                self._pipeline_stage_order.append(sid)
                self._pipeline_stages[sid] = _PipelineStageState(sid, str(label))
            self._record_progress_event(
                "progress_pipeline_configured",
                group_name=self._pipeline_group_name,
                stages=[
                    self._stage_progress_payload(self._pipeline_stages[stage_id])
                    for stage_id in self._pipeline_stage_order
                ],
            )
            self._plain_emit(
                f"Pipeline configured: {len(self._pipeline_stage_order)} stage(s)",
                force=True,
            )
            self._refresh()

    @progress_safe("start_stage")
    def start_stage(
        self,
        stage_id: str,
        label: str | None = None,
        *,
        total_steps: int | None = None,
        detail: str = "",
    ) -> None:
        with self._lock:
            stage = self._set_pipeline_stage_status(
                stage_id,
                StageStatus.RUNNING,
                label=label,
                detail=detail,
                create=True,
            )
            if total_steps is not None:
                self._set_phase_locked(stage.label, total_steps, stage_id=stage_id)
            else:
                self._phase_name = stage.label
                self._phase_id = stage_id
                self._phase_current = 0
                self._phase_total = 0
                self._phase_detail = detail
                self._reset_stage_metrics()
                self._refresh()

    @progress_safe("complete_stage")
    def complete_stage(self, stage_id: str, detail: str = "") -> None:
        with self._lock:
            self._set_pipeline_stage_status(
                stage_id,
                StageStatus.COMPLETED,
                detail=detail,
                create=True,
            )
            self._refresh()

    @progress_safe("skip_stage")
    def skip_stage(self, stage_id: str, detail: str = "") -> None:
        with self._lock:
            self._set_pipeline_stage_status(
                stage_id,
                StageStatus.SKIPPED,
                detail=detail,
                create=True,
            )
            self._refresh()

    @progress_safe("fail_stage")
    def fail_stage(self, stage_id: str, detail: str = "") -> None:
        with self._lock:
            self._set_pipeline_stage_status(
                stage_id,
                StageStatus.FAILED,
                detail=detail,
                create=True,
            )
            self._refresh()

    @progress_safe("cancel_stage")
    def cancel_stage(self, stage_id: str, detail: str = "") -> None:
        with self._lock:
            self._set_pipeline_stage_status(
                stage_id,
                StageStatus.CANCELLED,
                detail=detail,
                create=True,
            )
            self._refresh()

    def _set_pipeline_stage_status(
        self,
        stage_id: str,
        status: StageStatus,
        *,
        label: str | None = None,
        detail: str = "",
        create: bool = False,
    ) -> _PipelineStageState:
        stage = self._pipeline_stages.get(stage_id)
        if stage is None:
            if not create:
                return _PipelineStageState(stage_id, label or stage_id)
            stage = _PipelineStageState(stage_id, label or stage_id)
            self._pipeline_stages[stage_id] = stage
            self._pipeline_stage_order.append(stage_id)
        if label:
            stage.label = label
        if stage.status != status:
            stage.last_transition_at = time.time()
        stage.status = status
        stage.detail = detail
        self._record_progress_event(
            "progress_stage_update",
            stage=self._stage_progress_payload(stage),
        )
        self._plain_emit(_plain_stage_line(stage), force=True)
        return stage

    @progress_safe("phase")
    def phase(self, name: str) -> None:
        with self._lock:
            sid = str(name)
            if sid not in self._pipeline_stages:
                self._pipeline_stages[sid] = _PipelineStageState(sid, sid)
                self._pipeline_stage_order.append(sid)
            self._set_pipeline_stage_status(sid, StageStatus.RUNNING, create=True)
            self._phase_name = sid
            self._phase_id = sid
            self._phase_current = 0
            self._phase_total = 0
            self._phase_detail = ""
            self._subtask_group_name = f"{sid} tasks"
            self._subtask_display_mode = "all"
            self._archive_finished_tasks_locked()
            self._tasks.clear()
            self._reset_stage_metrics()
            self._last_activity = time.monotonic()
            self._record_progress_event("progress_phase_set", **self._phase_progress_payload())
            self._plain_emit(f"Phase started: {sid}", force=True)
            self._refresh()

    @progress_safe("set_phase")
    def set_phase(
        self,
        phase_name: str,
        total_steps: int,
        stage_id: str | None = None,
    ) -> None:
        with self._lock:
            self._set_phase_locked(phase_name, total_steps, stage_id=stage_id)
            self._refresh()

    def _set_phase_locked(
        self,
        phase_name: str,
        total_steps: int,
        *,
        stage_id: str | None = None,
    ) -> None:
        self._phase_name = phase_name
        self._phase_id = stage_id
        self._phase_current = 0
        self._phase_total = max(total_steps, 0)
        self._phase_detail = ""
        self._activity = ""
        self._archive_finished_tasks_locked()
        self._tasks.clear()
        self._subtask_group_name = f"{phase_name} tasks"
        self._subtask_display_mode = "all"
        self._errors.clear()
        self._reset_stage_metrics()
        if stage_id is not None and stage_id in self._pipeline_stages:
            self._set_pipeline_stage_status(stage_id, StageStatus.RUNNING, create=False)
        self._record_progress_event("progress_phase_set", **self._phase_progress_payload())
        self._plain_emit(
            f"Phase started: {phase_name} (0/{max(total_steps, 0)})",
            force=True,
        )

    @progress_safe("advance_phase")
    def advance_phase(self, amount: int = 1) -> None:
        with self._lock:
            if self._phase_total > 0:
                self._phase_current = min(self._phase_current + amount, self._phase_total)
            else:
                self._phase_current += amount
            self._plain_emit(
                f"Phase progress: {self._phase_name} "
                f"({self._phase_current}/{self._phase_total})"
            )
            self._record_progress_event(
                "progress_phase_advanced",
                amount=amount,
                **self._phase_progress_payload(),
            )
            self._refresh()

    @progress_safe("set_phase_detail")
    def set_phase_detail(self, detail: str) -> None:
        with self._lock:
            self._phase_detail = detail
            self._plain_emit(f"Phase detail: {self._phase_name} {detail}", force=True)
            self._record_progress_event(
                "progress_phase_detail",
                **self._phase_progress_payload(),
            )
            self._refresh()

    @progress_safe("set_phase_counter")
    def set_phase_counter(self, phase_name: str, total_steps: int) -> None:
        with self._lock:
            self._phase_name = phase_name
            self._phase_current = 0
            self._phase_total = max(total_steps, 0)
            self._phase_detail = ""
            self._plain_emit(
                f"Phase counter reset: {phase_name} (0/{self._phase_total})",
                force=True,
            )
            self._record_progress_event(
                "progress_phase_counter",
                **self._phase_progress_payload(),
            )
            self._refresh()

    @progress_safe("clear_phase")
    def clear_phase(self) -> None:
        with self._lock:
            self._phase_name = ""
            self._phase_current = 0
            self._phase_total = 0
            self._phase_detail = ""
            self._plain_emit("Phase cleared", force=True)
            self._record_progress_event(
                "progress_phase_cleared",
                **self._phase_progress_payload(),
            )
            self._refresh()

    @progress_safe("set_activity")
    def set_activity(self, text: str) -> None:
        with self._lock:
            self._activity = text
            self._last_activity = time.monotonic()
            self._record_progress_event("progress_activity", activity=text)
            self._plain_emit(f"Activity: {text}", force=True)
            self._refresh()

    @progress_safe("clear_activity")
    def clear_activity(self) -> None:
        with self._lock:
            self._activity = ""
            self._record_progress_event("progress_activity_cleared")
            self._refresh()

    @progress_safe("add_group")
    def add_group(
        self,
        group_id: str,
        label: str,
        *,
        phase: str | None = None,
        total: int | None = None,
    ) -> None:
        with self._lock:
            order = self._groups[group_id].order if group_id in self._groups else len(self._groups)
            self._groups[group_id] = _GroupState(
                label=label,
                phase=phase or self._phase_id or self._phase_name or "",
                total=total,
                order=order,
            )
            if not self._subtask_group_name:
                self._subtask_group_name = "Tasks"
            self._record_progress_event(
                "progress_group_update",
                group_id=group_id,
                label=label,
                phase=phase,
                total=total,
            )
            self._refresh()

    @progress_safe("set_subtask_caps")
    def set_subtask_caps(
        self,
        *,
        finished: int | None = None,
        pending: int | None = None,
    ) -> None:
        with self._lock:
            if finished is not None:
                self.MAX_VISIBLE_FINISHED = max(int(finished), 0)
            if pending is not None:
                self.MAX_VISIBLE_PENDING = max(int(pending), 0)
            self._record_progress_event(
                "progress_subtask_caps",
                max_visible_finished=self.MAX_VISIBLE_FINISHED,
                max_visible_pending=self.MAX_VISIBLE_PENDING,
            )
            self._refresh()

    @progress_safe("set_subtask_group")
    def set_subtask_group(
        self,
        group_name: str,
        tasks: list[dict[str, str]],
        *,
        display_mode: str = "all",
    ) -> None:
        with self._lock:
            if display_mode not in _TASK_DISPLAY_MODES:
                display_mode = "all"
            self._subtask_group_name = group_name
            self._subtask_display_mode = display_mode
            self._archive_finished_tasks_locked()
            self._tasks = {
                t["task_id"]: _TaskState(
                    task_id=t["task_id"],
                    label=t.get("label", t["task_id"]),
                    parent_id=t.get("parent_id", ""),
                    group=t.get("group", ""),
                )
                for t in tasks
            }
            mode_note = (
                " current batch only"
                if self._subtask_display_mode in {"current_batch", "active"}
                else ""
            )
            self._record_progress_event(
                "progress_subtasks_configured",
                group_name=self._subtask_group_name,
                display_mode=self._subtask_display_mode,
                tasks=[self._task_progress_payload(task) for task in self._tasks.values()],
            )
            self._plain_emit(
                f"Subtasks configured: {group_name} ({len(self._tasks)} task(s)){mode_note}",
                force=True,
            )
            self._refresh()

    @progress_safe("ensure_task")
    def ensure_task(
        self,
        task_id: str,
        label: str | None = None,
        *,
        parent_id: str | None = None,
        group: str | None = None,
    ) -> None:
        with self._lock:
            if task_id in self._tasks:
                task = self._tasks[task_id]
                if label:
                    task.label = label
                if parent_id is not None:
                    task.parent_id = parent_id
                if group is not None:
                    task.group = group
                self._record_progress_event(
                    "progress_task_declared",
                    task=self._task_progress_payload(task),
                )
                return
            task = _TaskState(
                task_id=task_id,
                label=label or task_id,
                parent_id=parent_id or "",
                group=group or "",
            )
            self._tasks[task_id] = task
            self._record_progress_event(
                "progress_task_declared",
                task=self._task_progress_payload(task),
            )
            self._plain_emit(f"Task declared: {label or task_id}")
            self._refresh()

    @progress_safe("start_task")
    def start_task(
        self,
        task_id: str,
        label: str,
        *,
        group: str = "",
        detail: str = "",
    ) -> None:
        with self._lock:
            self._last_activity = time.monotonic()
            if group and group not in self._groups:
                self.add_group(group, group)
            task = self._tasks.get(task_id)
            if task is None:
                task = _TaskState(task_id=task_id, label=label, group=group)
                self._tasks[task_id] = task
                self._counts["started"] += 1
            task.label = label
            task.group = group
            task.detail = detail
            task.status = TaskStatus.RUNNING
            task.last_transition_at = time.time()
            self._ensure_group_uses_task_units(group)
            self._record_progress_event(
                "progress_task_update",
                task=self._task_progress_payload(task),
            )
            self._plain_emit(_plain_task_line(task), force=True)
            self._refresh()

    @progress_safe("update_task")
    def update_task(
        self,
        task_id: str,
        status: str | None = None,
        detail: str = "",
        submitted: str | None = None,
    ) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                self.ensure_task(task_id)
                task = self._tasks.get(task_id)
                if task is None:
                    return
            new_status = _coerce_task_status(status or task.status.value)
            status_changed = task.status != new_status
            old_detail = task.detail
            old_submitted = task.submitted
            if status_changed:
                task.last_transition_at = time.time()
            task.status = new_status
            task.detail = detail
            if submitted is not None:
                task.submitted = submitted
            if (
                status_changed
                or old_detail != task.detail
                or (submitted is not None and old_submitted != task.submitted)
            ):
                self._record_progress_event(
                    "progress_task_update",
                    task=self._task_progress_payload(task),
                )
                self._plain_emit(
                    _plain_task_line(task),
                    force=new_status in _FINISHED_TASK_STATUSES,
                )
            self._refresh()

    @progress_safe("retry_task")
    def retry_task(self, task_id: str, *, detail: str = "") -> None:
        with self._lock:
            self._last_activity = time.monotonic()
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.status = TaskStatus.RUNNING
            task.detail = detail or task.detail
            task.last_transition_at = time.time()
            self._counts["retry"] += 1
            self._record_progress_event(
                "progress_task_retry",
                task=self._task_progress_payload(task),
                retry_count=self._counts["retry"],
            )
            self._plain_emit(_plain_task_line(task), force=True)
            self._refresh()

    @progress_safe("finish_task")
    def finish_task(
        self,
        task_id: str,
        *,
        ok: bool = True,
        anomaly: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            self._last_activity = time.monotonic()
            task = self._tasks.get(task_id)
            if task is None:
                return
            previous = task.status
            task.status = TaskStatus.COMPLETED if ok else TaskStatus.FAILED
            task.last_transition_at = time.time()
            task.anomaly = anomaly or ""
            if detail is not None:
                task.detail = detail
            if previous not in _FINISHED_TASK_STATUSES:
                self._counts["ok" if ok else "fail"] += 1
            elif previous == TaskStatus.COMPLETED and not ok:
                self._counts["ok"] = max(0, self._counts["ok"] - 1)
                self._counts["fail"] += 1
            elif previous == TaskStatus.FAILED and ok:
                self._counts["fail"] = max(0, self._counts["fail"] - 1)
                self._counts["ok"] += 1
            self._record_progress_event(
                "progress_task_update",
                task=self._task_progress_payload(task),
            )
            self._plain_emit(_plain_task_line(task), force=True)
            self._refresh()

    def count_subtasks_by_parent(self, parent_id: str) -> tuple[int, int, int, int]:
        children = [task for task in self._tasks.values() if task.parent_id == parent_id]
        total = len(children)
        done = sum(1 for task in children if task.status in _FINISHED_TASK_STATUSES)
        running = sum(1 for task in children if task.status == TaskStatus.RUNNING)
        failed = sum(
            1
            for task in children
            if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}
        )
        return total, done, running, failed

    @progress_safe("report_error")
    def report_error(self, task_id: str, message: str) -> None:
        with self._lock:
            stamp = time.strftime("%H:%M")
            self._errors.append(f"[{stamp}] {task_id}: {message}")
            self._record_progress_event(
                "progress_error",
                task_id=task_id,
                message=message,
                error_count=len(self._errors),
            )
            self._plain_emit(f"ERROR {task_id}: {message}", force=True)
            self._refresh()

    @progress_safe("note_llm_retry")
    def note_llm_retry(
        self,
        reason: str,
        attempt: int,
        next_wait_s: float,
        exc_type: str,
        max_attempts: int | None = None,
    ) -> None:
        label_by_reason = {
            "api_transient": "upstream error",
            "network_transient": "provider retry",
            "rate_limit": "rate limit",
            "empty_content": "empty response",
            "structured_parse": "structured parse",
            "tool_parse": "tool parse",
            "structured_transport": "structured output fallback",
            "structured_json": "structured parse",
        }
        label = label_by_reason.get(reason, reason)
        next_wait_s = max(float(next_wait_s), 0.0)
        wait_fragment = f", waiting {next_wait_s:.0f}s" if next_wait_s > 0 else ""
        now = time.time()
        if self._llm_retry_started_at == 0.0:
            self._llm_retry_started_at = now
        elapsed_s = now - self._llm_retry_started_at
        elapsed_fragment = (
            f", elapsed {_fmt_duration(elapsed_s)}"
            if elapsed_s >= 1.0 and reason not in _LLM_PROVIDER_RETRY_REASONS
            else ""
        )
        cap = max_attempts or _LLM_BOUNDED_RETRY_CAPS.get(reason)
        attempt_fragment = f"{attempt}/{cap}" if cap is not None else f"{attempt}"
        provider_budget_fragment = (
            ", no time cap" if reason in _LLM_PROVIDER_RETRY_REASONS else ""
        )
        self._llm_retry_status = (
            f"LLM retry ({label}, {exc_type}) — attempt {attempt_fragment}"
            f"{wait_fragment}{provider_budget_fragment}{elapsed_fragment}"
        )
        self._llm_retry_updated_at = now
        self._llm_retry_attempt = attempt
        self._llm_retry_reason = reason
        self._llm_retry_next_wait_s = next_wait_s
        self._llm_retry_visible_until_at = now + max(
            _LLM_RETRY_STATUS_TTL_S,
            next_wait_s + 2.0,
        )
        self._record_progress_event(
            "progress_llm_retry",
            reason=reason,
            label=label,
            attempt=attempt,
            max_attempts=cap,
            next_wait_s=next_wait_s,
            exc_type=exc_type,
            status=self._llm_retry_status,
        )
        self._plain_emit(self._llm_retry_status, force=True)
        self._refresh()

    @progress_safe("note_llm_ok")
    def note_llm_ok(self) -> None:
        if not self._llm_retry_status:
            return
        self._llm_retry_status = ""
        self._llm_retry_updated_at = 0.0
        self._llm_retry_attempt = 0
        self._llm_retry_reason = ""
        self._llm_retry_next_wait_s = 0.0
        self._llm_retry_visible_until_at = 0.0
        self._llm_retry_started_at = 0.0
        self._record_progress_event("progress_llm_ok")
        self._refresh()

    @progress_safe("note_llm_provider_wait")
    def note_llm_provider_wait(
        self,
        provider_name: str,
        next_provider_name: str | None,
        wait_s: float,
        reason: str,
    ) -> None:
        now = time.time()
        if self._llm_provider_started_at == 0.0:
            self._llm_provider_started_at = now
        reason = reason[:180]
        if next_provider_name:
            self._llm_provider_status = (
                f"LLM provider unavailable ({provider_name}); "
                f"switching to {next_provider_name}. {reason}"
            )
        elif wait_s > 0:
            self._llm_provider_status = (
                f"LLM providers unavailable; waiting {wait_s:.0f}s. {reason}"
            )
        else:
            self._llm_provider_status = (
                f"LLM provider unavailable ({provider_name}). {reason}"
            )
        self._llm_provider_updated_at = now
        self._llm_provider_visible_until_at = now + max(
            _LLM_PROVIDER_STATUS_TTL_S,
            max(float(wait_s), 0.0) + 2.0,
        )
        self._record_progress_event(
            "progress_llm_provider_wait",
            provider_name=provider_name,
            next_provider_name=next_provider_name,
            wait_s=wait_s,
            reason=reason,
            status=self._llm_provider_status,
        )
        self._plain_emit(self._llm_provider_status, force=True)
        self._refresh()

    @progress_safe("note_llm_provider_ok")
    def note_llm_provider_ok(self) -> None:
        if not self._llm_provider_status:
            return
        self._llm_provider_status = ""
        self._llm_provider_updated_at = 0.0
        self._llm_provider_started_at = 0.0
        self._llm_provider_visible_until_at = 0.0
        self._record_progress_event("progress_llm_provider_ok")
        self._refresh()

    @progress_safe("update_cost")
    def update_cost(
        self,
        cost_usd: float | None,
        prompt_tokens: int,
        completion_tokens: int,
        llm_calls: int = 1,
        cost_source: str | None = None,
        call_id: str | None = None,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
    ) -> None:
        with self._lock:
            cost_value = float(cost_usd or 0.0)
            self._cost_usd += cost_value
            self._prompt_tokens += prompt_tokens
            self._completion_tokens += completion_tokens
            self._cache_hit_tokens += cache_hit_tokens
            self._cache_miss_tokens += cache_miss_tokens
            self._llm_calls += llm_calls
            self._total_cost_usd += cost_value
            self._total_prompt_tokens += prompt_tokens
            self._total_completion_tokens += completion_tokens
            self._total_cache_hit_tokens += cache_hit_tokens
            self._total_cache_miss_tokens += cache_miss_tokens
            self._total_llm_calls += llm_calls
            self._last_llm_call_time = time.time()

            effective_source = cost_source or (
                "api" if cost_usd is not None else "token_usage_only"
            )
            self._cost_sources_seen.add(effective_source)
            self._plain_emit(
                "Usage: "
                f"calls={self._total_llm_calls} "
                f"tokens={_fmt_tokens(self._total_prompt_tokens)}/"
                f"{_fmt_tokens(self._total_completion_tokens)} "
                f"cost={self._format_cost_label(self._total_cost_usd)}"
            )
            self._record_progress_event(
                "progress_usage_update",
                cost_source=effective_source,
                cost_usd=cost_usd,
                call_id=call_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_hit_tokens=cache_hit_tokens,
                cache_miss_tokens=cache_miss_tokens,
                llm_calls=llm_calls,
                **self._metrics_progress_payload(),
            )
            self._refresh()

    @progress_safe("finish")
    def finish(self) -> None:
        self.stop()
        self._record_progress_event("progress_finish", **self._metrics_progress_payload())
        if self._mode == "off":
            return
        elapsed = time.time() - self._pipeline_start_time
        message = (
            f"\n[bold green]Pipeline complete.[/]  "
            f"Elapsed: {_fmt_duration(elapsed)} | "
            f"Cost: {self._format_cost_label(self._total_cost_usd)} | "
            f"LLM calls: {self._total_llm_calls}"
        )
        note = self._cost_unavailable_note()
        if note:
            message += f"\n[dim]{note}[/]"
        self._console.print(message)

    def is_cost_available(self) -> bool:
        return "api" in self._cost_sources_seen

    def format_total_cost(self) -> str:
        return self._format_cost_label(self._total_cost_usd)

    def cost_unavailable_note(self) -> str:
        return self._cost_unavailable_note()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            tasks = self._task_counts_locked()
            return {
                "run_id": self.run_id,
                "elapsed_s": round(time.time() - self._pipeline_start_time, 1),
                "phase": self._phase_id or self._phase_name,
                "tasks": tasks,
                "anomalies_by_kind": dict(self._anom_by_kind),
                "anomaly_total": sum(self._anom_by_kind.values()),
                "alerts_by_event": dict(self._alert_by_event),
                "alert_total": sum(self._alert_by_event.values()),
                "llm_calls": self._total_llm_calls,
                "prompt_tokens": self._total_prompt_tokens,
                "completion_tokens": self._total_completion_tokens,
                "total_tokens": self._total_prompt_tokens + self._total_completion_tokens,
                "cost": self._format_cost_label(self._total_cost_usd),
            }

    def _on_anomaly(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._last_activity = time.monotonic()
            kind = str(record.get("anomaly", "internal"))
            self._anom_by_kind[kind] = self._anom_by_kind.get(kind, 0) + 1
            self._anoms.append(record)
            self._alerts.append({"alert_kind": "anomaly", **record})
            self._record_progress_event(
                "progress_anomaly",
                anomaly=kind,
                record=_compact_alert_for_progress(record),
            )
            self.report_error(
                str(record.get("work_item_id") or record.get("agent") or "run"),
                str(record.get("message") or kind),
            )

    def _on_event(self, record: dict[str, Any]) -> None:
        event = str(record.get("event", ""))
        if event == "anomaly":
            return
        level = str(record.get("level", ""))
        if event not in _WATCH_EVENTS and level not in {"warning", "error"}:
            return
        with self._lock:
            self._last_activity = time.monotonic()
            self._alert_by_event[event] = self._alert_by_event.get(event, 0) + 1
            self._alerts.append({"alert_kind": "event", **record})
            self._record_progress_event(
                "progress_alert",
                event_name=event,
                level=level,
                record=_compact_alert_for_progress(record),
            )
            if level in {"warning", "error"}:
                self.report_error(
                    str(record.get("work_item_id") or record.get("agent") or event),
                    str(
                        record.get("reason")
                        or record.get("message")
                        or record.get("anomaly")
                        or event
                    )[:240],
                )

    def _ensure_group_uses_task_units(self, group: str) -> None:
        if not group:
            return
        group_state = self._groups.get(group)
        if group_state is None or group_state.total is None:
            return
        task_count = sum(1 for task in self._tasks.values() if task.group == group)
        if task_count > group_state.total:
            group_state.total = None

    def _refresh(self) -> None:
        try:
            live = self._live
            if self._mode == "rich" and live is not None:
                live.update(self._render())
        except Exception:
            try:
                self._log.warning("progress_refresh_failed")
            except Exception:
                pass

    def _plain_emit(self, line: str, *, force: bool = False) -> None:
        if getattr(self, "_mode", "rich") != "plain" or not getattr(self, "_started", False):
            return
        now = time.monotonic()
        signature = line
        if (
            not force
            and signature == self._plain_last_signature
            and now - self._plain_last_emit_at < _PLAIN_UPDATE_MIN_INTERVAL_S
        ):
            return
        if not force and now - self._plain_last_emit_at < _PLAIN_UPDATE_MIN_INTERVAL_S:
            return
        self._plain_last_emit_at = now
        self._plain_last_signature = signature
        try:
            print(f"[tend] {line}", file=sys.stderr, flush=True)
        except Exception:
            pass

    def _available_width(self) -> int:
        raw = max(self._console.width or 80, 80)
        return max(raw - 4, 76)

    def _subtask_column_widths(self, *, has_submitted: bool) -> tuple[int, int, int | None]:
        width = self._available_width()
        if has_submitted:
            task = max(36, min(width // 3, 80))
            submitted = max(34, min(width // 4, 56))
            status = max(40, width - task - submitted - 6)
            return task, status, submitted
        task = max(36, min(width // 3, 90))
        status = max(40, width - task - 4)
        return task, status, None

    def _pipeline_column_widths(self) -> tuple[int, int]:
        width = self._available_width()
        stage = max(30, min(width // 3, 60))
        status = max(46, width - stage - 4)
        return stage, status

    def _render_subtask_table(self) -> Table | None:
        if not self._tasks:
            return None
        show_submitted = any(task.submitted for task in self._tasks.values())
        task_w, status_w, submitted_w = self._subtask_column_widths(
            has_submitted=show_submitted
        )
        table = Table(
            title=self._subtask_group_name,
            show_header=False,
            box=None,
            padding=(0, 1),
            expand=True,
        )
        table.add_column(
            "Task",
            style="cyan",
            min_width=task_w,
            ratio=1,
            no_wrap=False,
            overflow="fold",
        )
        table.add_column(
            "Status",
            min_width=status_w,
            ratio=2 if not show_submitted else 3,
            no_wrap=False,
            overflow="fold",
        )
        if show_submitted:
            table.add_column(
                "Submitted",
                style="green",
                width=submitted_w or 34,
                no_wrap=True,
                overflow="fold",
            )

        current_batch_only = self._subtask_display_mode in {"current_batch", "active"}
        root_tasks, child_tasks = _split_task_tree(self._tasks)
        visible_roots, hidden_finished, hidden_pending = _visible_task_window(
            root_tasks,
            current_batch_only=current_batch_only,
            max_finished=self.MAX_VISIBLE_FINISHED,
            max_pending=self.MAX_VISIBLE_PENDING,
        )
        for task in visible_roots:
            _add_task_table_row(table, task, show_submitted=show_submitted, depth=0)
            self._add_visible_child_task_rows(
                table,
                child_tasks.get(task.task_id, []),
                children_by_parent_id=child_tasks,
                depth=1,
                current_batch_only=current_batch_only,
                show_submitted=show_submitted,
            )
        _add_task_summary_row(
            table,
            hidden_finished=hidden_finished,
            hidden_pending=hidden_pending,
            display_mode=self._subtask_display_mode,
            show_submitted=show_submitted,
            depth=0,
        )
        return table

    def _add_visible_child_task_rows(
        self,
        table: Table,
        children: list[_TaskState],
        *,
        children_by_parent_id: dict[str, list[_TaskState]],
        depth: int,
        current_batch_only: bool,
        show_submitted: bool,
    ) -> None:
        if not children:
            return
        visible_children, hidden_finished, hidden_pending = _visible_task_window(
            children,
            current_batch_only=current_batch_only,
            max_finished=self.MAX_VISIBLE_FINISHED,
            max_pending=self.MAX_VISIBLE_PENDING,
        )
        for child in visible_children:
            _add_task_table_row(table, child, show_submitted=show_submitted, depth=depth)
            grandchildren = children_by_parent_id.get(child.task_id, [])
            if grandchildren:
                self._add_visible_child_task_rows(
                    table,
                    grandchildren,
                    children_by_parent_id=children_by_parent_id,
                    depth=depth + 1,
                    current_batch_only=current_batch_only,
                    show_submitted=show_submitted,
                )
        _add_task_summary_row(
            table,
            hidden_finished=hidden_finished,
            hidden_pending=hidden_pending,
            display_mode=self._subtask_display_mode,
            show_submitted=show_submitted,
            depth=depth,
        )

    def _render_pipeline_overview(self) -> Table | None:
        if not self._pipeline_stage_order:
            return None
        stage_w, status_w = self._pipeline_column_widths()
        overview = Table(
            title=self._pipeline_group_name or "Pipeline stages",
            show_header=False,
            box=None,
            padding=(0, 1),
            expand=True,
        )
        overview.add_column(
            "Stage",
            style="cyan",
            min_width=stage_w,
            ratio=1,
            no_wrap=False,
            overflow="fold",
        )
        overview.add_column(
            "Status",
            min_width=status_w,
            ratio=2,
            no_wrap=False,
            overflow="fold",
        )
        for sid in self._pipeline_stage_order:
            stage = self._pipeline_stages.get(sid)
            if stage is None:
                continue
            icon = _STAGE_STATUS_ICONS.get(stage.status, "?")
            detail = f" {stage.detail}" if stage.detail else ""
            overview.add_row(
                stage.label,
                f"{icon} {stage.status.value.capitalize()}{detail}",
            )
        return overview

    def _render_phase_progress(self) -> RenderableType | None:
        if not self._phase_name:
            return None
        phase_line = Text.assemble((self._phase_name, "bold"))
        if self._phase_detail:
            phase_line.append(" ")
            phase_line.append(self._phase_detail)
        if self._last_llm_call_time > 0:
            idle_s = time.time() - self._last_llm_call_time
            idle_label = _fmt_duration(idle_s)
        else:
            idle_label = "--"
        progress_line = Text.assemble(
            ("Progress  ", "bold"),
            (
                _format_progress_bar(
                    self._phase_current,
                    self._phase_total,
                    width=_phase_bar_width(self._available_width()),
                ),
                "cyan",
            ),
            (f" {self._phase_current}/{self._phase_total} ", ""),
            (f"⏳ {idle_label}", "dim"),
        )
        return Group(phase_line, progress_line)

    def _render_activity_line(self) -> Text | None:
        if not self._activity:
            return None
        frame = _SPINNER_FRAMES[self._spinner_tick % len(_SPINNER_FRAMES)]
        self._spinner_tick += 1
        return Text.assemble("  ", (frame, "yellow"), " ", self._activity)

    def _render_llm_status_lines(self) -> list[Text]:
        lines: list[Text] = []
        retry_text = self._llm_retry_render_text()
        if retry_text:
            lines.append(Text.from_markup(retry_text))
        provider_text = self._llm_provider_render_text()
        if provider_text:
            lines.append(Text.from_markup(provider_text))
        return lines

    def _render_error_block(self) -> Text | None:
        if not self._errors:
            return None
        lines = "\n".join(self._errors)
        return Text.from_markup(
            f"[bold red]Errors (last {len(self._errors)}):[/]\n{lines}"
        )

    def _stage_usage_duplicates_total(self) -> bool:
        return (
            abs(self._cost_usd - self._total_cost_usd) < 1e-12
            and self._prompt_tokens == self._total_prompt_tokens
            and self._completion_tokens == self._total_completion_tokens
            and self._cache_hit_tokens == self._total_cache_hit_tokens
            and self._cache_miss_tokens == self._total_cache_miss_tokens
            and self._llm_calls == self._total_llm_calls
        )

    def _render_stage_stats_line(self) -> Text:
        phase_elapsed = time.time() - self._start_time
        p_in = _fmt_tokens(self._prompt_tokens)
        p_out = _fmt_tokens(self._completion_tokens)
        phase_cost = self._format_cost_label(self._cost_usd)
        cache_pct = int(100 * self._cache_hit_tokens / max(self._prompt_tokens, 1))
        duplicate_note = " │ same as total so far" if self._stage_usage_duplicates_total() else ""
        phase_line = (
            f"Stage  │ Cost: {phase_cost} │ "
            f"Tokens: {p_in}/{p_out} │ "
            f"Cache: {cache_pct}% │ "
            f"LLM calls: {self._llm_calls} │ "
            f"Duration: {_fmt_duration(phase_elapsed)}"
            f"{duplicate_note}"
        )
        return Text.from_markup(f"[dim]{phase_line}[/]")

    def _render_total_stats_line(self) -> Text:
        total_elapsed = time.time() - self._pipeline_start_time
        t_in = _fmt_tokens(self._total_prompt_tokens)
        t_out = _fmt_tokens(self._total_completion_tokens)
        total_cost = self._format_cost_label(self._total_cost_usd)
        cache_pct = int(100 * self._total_cache_hit_tokens / max(self._total_prompt_tokens, 1))
        total_line = (
            f"Total  │ Cost: {total_cost} │ "
            f"Tokens: {t_in}/{t_out} │ "
            f"Cache: {cache_pct}% │ "
            f"LLM calls: {self._total_llm_calls} │ "
            f"Duration: {_fmt_duration(total_elapsed)}"
        )
        return Text.from_markup(f"[dim]{total_line}[/]")

    def _render_panel_title(self) -> str:
        title = "TEND Solver Pipeline"
        if self._repo_name:
            title += f" ─── repo: {self._repo_name}"
        return f"{title} ── run: {self._run_id}"

    def _render(self) -> RenderableType:
        parts: list[RenderableType] = []
        optional_parts: list[RenderableType | None] = [
            self._render_pipeline_overview(),
            self._render_phase_progress(),
            self._render_activity_line(),
            *self._render_llm_status_lines(),
            self._render_subtask_table(),
            self._render_error_block(),
            self._render_stage_stats_line(),
            self._render_total_stats_line(),
        ]
        parts.extend(part for part in optional_parts if part is not None)
        return Panel(Group(*parts), title=self._render_panel_title(), border_style="blue")

    def _llm_retry_render_text(self) -> str:
        if not self._llm_retry_status:
            return ""
        now = time.time()
        age = now - self._llm_retry_updated_at
        visible_until = self._llm_retry_visible_until_at or (
            self._llm_retry_updated_at + _LLM_RETRY_STATUS_TTL_S
        )
        if now > visible_until:
            return ""
        if self._llm_retry_reason in _LLM_PROVIDER_RETRY_REASONS:
            retry_elapsed = now - (self._llm_retry_started_at or self._llm_retry_updated_at)
            fragments = [
                f"retrying for {_fmt_duration(retry_elapsed)} (no time cap)",
                f"{_fmt_duration(age)} since last attempt",
            ]
        else:
            fragments = [f"{_fmt_duration(age)} since last attempt"]
        if self._llm_retry_next_wait_s > 0:
            retry_at = self._llm_retry_updated_at + self._llm_retry_next_wait_s
            remaining = max(retry_at - now, 0.0)
            if remaining >= 1.0:
                fragments.append(f"{_fmt_duration(remaining)} until next retry")
        retry_fragment = " [" + " | ".join(fragments) + "]"
        return f"  [red]⚠[/] [yellow]{self._llm_retry_status}[/][dim]{retry_fragment}[/]"

    def _llm_provider_render_text(self) -> str:
        if not self._llm_provider_status:
            return ""
        now = time.time()
        visible_until = self._llm_provider_visible_until_at or (
            self._llm_provider_updated_at + _LLM_PROVIDER_STATUS_TTL_S
        )
        if now > visible_until:
            return ""
        total = now - self._llm_provider_started_at
        total_fragment = f" [{_fmt_duration(total)} provider wait]" if total >= 1.0 else ""
        return f"  [red]⚠[/] [yellow]{self._llm_provider_status}[/][dim]{total_fragment}[/]"

    def _record_progress_event(self, event: str, **fields: Any) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "repo": self._repo_name,
            "event": event,
            **fields,
        }
        self._progress_sink.write(payload, source_event=event)

    def _task_counts_locked(self) -> dict[str, int]:
        current = self._task_counts_for(self._tasks.values())
        ok = self._archived_task_counts["ok"] + current["ok"]
        fail = self._archived_task_counts["fail"] + current["fail"]
        running = current["running"]
        started = self._archived_task_counts["started"] + current["started"]
        return {
            "started": started,
            "ok": ok,
            "fail": fail,
            "retry": self._counts["retry"],
            "running": running,
        }

    def _archive_finished_tasks_locked(self) -> None:
        counts = self._task_counts_for(self._tasks.values())
        self._archived_task_counts["ok"] += counts["ok"]
        self._archived_task_counts["fail"] += counts["fail"]
        self._archived_task_counts["started"] += counts["ok"] + counts["fail"]

    @staticmethod
    def _task_counts_for(tasks: Any) -> dict[str, int]:
        materialized = list(tasks)
        ok = sum(1 for task in materialized if task.status == TaskStatus.COMPLETED)
        fail = sum(
            1
            for task in materialized
            if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}
        )
        running = sum(1 for task in materialized if task.status == TaskStatus.RUNNING)
        started = ok + fail + running
        return {
            "started": started,
            "ok": ok,
            "fail": fail,
            "running": running,
        }

    def _phase_progress_payload(self) -> dict[str, Any]:
        return {
            "phase_name": self._phase_name,
            "phase_id": self._phase_id,
            "phase_current": self._phase_current,
            "phase_total": self._phase_total,
            "phase_detail": self._phase_detail,
        }

    @staticmethod
    def _task_progress_payload(task: _TaskState) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "label": task.label,
            "parent_id": task.parent_id,
            "group": task.group,
            "status": task.status.value,
            "detail": task.detail,
            "submitted": task.submitted,
            "anomaly": task.anomaly,
        }

    @staticmethod
    def _stage_progress_payload(stage: _PipelineStageState) -> dict[str, Any]:
        return {
            "stage_id": stage.stage_id,
            "label": stage.label,
            "status": stage.status.value,
            "detail": stage.detail,
        }

    def _metrics_progress_payload(self) -> dict[str, Any]:
        return {
            "phase_cost_usd": self._cost_usd,
            "phase_prompt_tokens": self._prompt_tokens,
            "phase_completion_tokens": self._completion_tokens,
            "phase_cache_hit_tokens": self._cache_hit_tokens,
            "phase_cache_miss_tokens": self._cache_miss_tokens,
            "phase_llm_calls": self._llm_calls,
            "total_cost_usd": self._total_cost_usd,
            "total_prompt_tokens": self._total_prompt_tokens,
            "total_completion_tokens": self._total_completion_tokens,
            "total_cache_hit_tokens": self._total_cache_hit_tokens,
            "total_cache_miss_tokens": self._total_cache_miss_tokens,
            "total_llm_calls": self._total_llm_calls,
            "cost_sources_seen": sorted(self._cost_sources_seen),
        }

    def _format_cost_label(self, cost_usd: float) -> str:
        if self._cost_sources_seen and "api" not in self._cost_sources_seen:
            return "N/A"
        return f"${cost_usd:.2f}"

    def _cost_unavailable_note(self) -> str:
        if self._cost_sources_seen and "api" not in self._cost_sources_seen:
            return (
                "Cost shown as N/A: the configured LLM provider does not "
                "return per-call cost in its response. Check billing on "
                "the provider dashboard for the authoritative total."
            )
        return ""

    def _reset_stage_metrics(self) -> None:
        self._cost_usd = 0.0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cache_hit_tokens = 0
        self._cache_miss_tokens = 0
        self._llm_calls = 0
        self._start_time = time.time()


def make_reporter(run_id: str, logger: RunLogger, *, enabled: bool = True) -> ProgressReporter:
    return ProgressReporter(run_id, logger, enabled=enabled)


def _compact_alert_for_progress(record: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "alert_kind",
        "ts",
        "level",
        "event",
        "anomaly",
        "db_id",
        "record_id",
        "agent",
        "call_id",
        "model",
        "phase",
        "task_id",
        "work_item_id",
        "session_id",
        "agent_session_ref",
        "transcript_ref",
        "diagnostics_ref",
        "error_code",
        "prompt_chars",
        "threshold_chars",
    )
    compact = {key: record.get(key) for key in keep if record.get(key) is not None}
    for key in ("reason", "message"):
        value = record.get(key)
        if value is not None:
            compact[key] = _truncate_progress_text(str(value), 240)
    return compact


def _truncate_progress_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "..."


__all__ = [
    "ProgressReporter",
    "TaskStatus",
    "StageStatus",
    "make_reporter",
]
