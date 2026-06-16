from __future__ import annotations

# ruff: noqa: F403,F405

from tend.utils.logging._config import *

from tend.utils.logging._paths import *
from tend.utils.logging._stage_logger import StageLogger
from tend.utils.logging._task_logger import TaskLogger

def _create_file_logger(
    name: str, path: Path, level: int = logging.DEBUG
) -> structlog.stdlib.BoundLogger:
    path.parent.mkdir(parents=True, exist_ok=True)
    stdlib_logger = logging.getLogger(name)
    stdlib_logger.setLevel(level)
    stdlib_logger.propagate = False

    for h in stdlib_logger.handlers[:]:
        stdlib_logger.removeHandler(h)

    handler = logging.FileHandler(str(path), encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(_get_json_formatter())
    stdlib_logger.addHandler(handler)

    return structlog.get_logger(name)

class _ErrorTeeFilter(logging.Filter):
    """Tee every ``tend.*`` ERROR+ record into the run ``errors.jsonl``.

    ``errors.jsonl`` was only ever written from
    :meth:`LogManager.log_exception_event`, so module-logger CRITICALs and
    handled ``logger.error(...)`` calls that never went through that method
    bypassed the central error index. Attaching this filter to the
    milestones handler makes ``errors.jsonl`` the universal ERROR index:
    any record with ``levelno >= ERROR`` and a ``tend.`` logger name is
    appended (under the shared error lock, reusing the JSON formatter) in
    addition to its normal handling. The filter never suppresses a record
    — it always returns ``True`` so the milestones stream is unaffected.
    """

    def __init__(self, manager: LogManager) -> None:
        super().__init__()
        self._manager = manager

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR and record.name.startswith("tend."):
            try:
                self._manager._tee_error_record(record)
            except Exception:
                # The tee is best-effort observability — never let it break
                # the underlying logging call it is attached to.
                pass
        return True

class LogManager:
    """Manages per-run log directory tree and provides isolated loggers.

    Usage::

        mgr = LogManager("2026-03-25_1430")
        stage_log = mgr.get_stage_logger("b2")
        task_log  = mgr.get_task_logger("b2", "layer0/model_revenue")
    """

    def __init__(
        self,
        run_id: str,
        base_dir: str | Path = "logs",
        *,
        command: str = "",
    ) -> None:
        _ensure_structlog_configured()
        self.run_id = run_id
        self.command = command
        self.root = Path(base_dir) / run_id
        self.root.mkdir(parents=True, exist_ok=True)

        self._run_logger = _create_file_logger(
            f"tend.run.{run_id}",
            self.root / "run.log",
            level=logging.INFO,
        )
        self._run_log_path = self.root / "run.log"
        self._cost_path = self.root / "cost_summary.jsonl"
        self._errors_path = self.root / "errors.jsonl"
        self._summary_path = self.root / "run_summary.json"
        self._error_count = 0
        self._error_lock = threading.Lock()
        self._cost_lock = threading.Lock()
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._logger_counter = 0

        root = logging.getLogger()
        root_handler = logging.FileHandler(
            str(self._run_log_path),
            encoding="utf-8",
        )
        self._root_handler: logging.FileHandler | None = root_handler
        root_handler.setLevel(logging.WARNING)
        root_handler.setFormatter(_get_json_formatter())
        root_handler.addFilter(
            _RunContextFilter(run_id=self.run_id, command=self.command)
        )
        root.addHandler(root_handler)

        # Milestones stream: a dedicated INFO-level sink that captures every
        # first-party ``tend.*`` module-logger event. Module loggers
        # (``get_logger(__name__)``) propagate to root but were silently
        # dropped because the only root handler is WARNING+ AND the inherited
        # logger level is WARNING (so INFO records were never even created).
        # We raise the ``tend`` logger to INFO (records get created) and add
        # a name-filtered INFO handler (records get persisted), so seed/loop/D2
        # INFO milestones land in one greppable file with no per-callsite
        # wiring. The WARNING run.log handler is unchanged. See
        # ``tend.utils.task_event.emit_task_event`` for the per-task variant.
        tend_logger = logging.getLogger("tend")
        self._prev_tend_level = tend_logger.level
        if tend_logger.level == logging.NOTSET or tend_logger.level > logging.INFO:
            tend_logger.setLevel(logging.INFO)
        self._milestones_path = self.root / "milestones.jsonl"
        milestones_handler = logging.FileHandler(
            str(self._milestones_path),
            encoding="utf-8",
        )
        self._milestones_handler: logging.FileHandler | None = milestones_handler
        milestones_handler.setLevel(logging.INFO)
        milestones_handler.setFormatter(_get_json_formatter())
        milestones_handler.addFilter(_NamePrefixFilter("tend."))
        milestones_handler.addFilter(
            _RunContextFilter(run_id=self.run_id, command=self.command)
        )
        # Tee ERROR+ records into errors.jsonl so it is the universal error
        # index, not just the subset that flows through log_exception_event.
        # Registered last so the run-context filter has already stamped the
        # record by the time the tee serialises it. The tee always returns
        # True; it never drops a record from the milestones stream.
        milestones_handler.addFilter(_ErrorTeeFilter(self))
        root.addHandler(milestones_handler)

    def close(self) -> None:
        """Flush and remove the root safety-net + milestones handlers."""
        if self._root_handler is not None:
            self._root_handler.flush()
            logging.getLogger().removeHandler(self._root_handler)
            self._root_handler.close()
            self._root_handler = None
        if self._milestones_handler is not None:
            self._milestones_handler.flush()
            logging.getLogger().removeHandler(self._milestones_handler)
            self._milestones_handler.close()
            self._milestones_handler = None
            logging.getLogger("tend").setLevel(self._prev_tend_level)

    # ------------------------------------------------------------------

    def _unique_logger_name(self, base: str) -> str:
        self._logger_counter += 1
        return f"tend.{self.run_id}.{base}.{self._logger_counter}"

    # ------------------------------------------------------------------
    # Logger factories
    # ------------------------------------------------------------------

    def get_stage_logger(self, stage: str) -> StageLogger:
        """Logger for stage-level lifecycle events (started/completed/failed)."""
        stage_dir = self.root / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        logger = _create_file_logger(
            self._unique_logger_name(stage),
            stage_dir / f"{stage}.log",
        )
        return StageLogger(
            stage=stage,
            logger=logger,
            manager=self,
            log_path=stage_dir / f"{stage}.log",
        )

    def get_task_logger(self, stage: str, task_id: str) -> TaskLogger:
        """Logger for a single parallel unit (model, instance, iteration)."""
        stage_dir = self.root / stage
        stage_dir.mkdir(parents=True, exist_ok=True)

        log_path = _safe_task_log_path(stage_dir, task_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        logger = _create_file_logger(
            self._unique_logger_name(f"{stage}.{task_id}"),
            log_path,
        )
        llm_dir = stage_dir / "llm"
        llm_dir.mkdir(parents=True, exist_ok=True)
        return TaskLogger(
            stage=stage,
            task_id=task_id,
            logger=logger,
            llm_dir=llm_dir,
            manager=self,
            log_path=log_path,
        )

    def get_nested_task_logger(
        self, stage: str, parent_id: str, task_id: str
    ) -> TaskLogger:
        """Logger for double-nested parallelism (e.g. instance/{id}/step_b/{model})."""
        parent_parts = [
            safe_dirname(p) for p in re.split(r"[\\/]+", parent_id) if p
        ] or ["_"]
        task_dir = self.root.joinpath(stage, *parent_parts)
        task_dir.mkdir(parents=True, exist_ok=True)

        log_path = _safe_task_log_path(task_dir, task_id)
        logger = _create_file_logger(
            self._unique_logger_name(f"{stage}.{parent_id}.{task_id}"),
            log_path,
        )
        llm_dir = task_dir / "llm"
        llm_dir.mkdir(parents=True, exist_ok=True)
        return TaskLogger(
            stage=stage,
            task_id=f"{parent_id}/{task_id}",
            logger=logger,
            llm_dir=llm_dir,
            manager=self,
            log_path=log_path,
        )

    def get_branch_logger(
        self,
        wave: int,
        branch_id: str,
    ) -> TaskLogger:
        """Convenience wrapper for creating a branch-scoped task logger."""
        return self.get_task_logger(
            "p2",
            f"wave_{wave:02d}_branch_{branch_id}",
        )

    # ------------------------------------------------------------------
    # Run-level events
    # ------------------------------------------------------------------

    def log_run_event(
        self,
        event_name: str,
        *,
        _level: str = "info",
        **kwargs: Any,
    ) -> None:
        payload = _sanitize_log_kwargs(kwargs)
        payload.setdefault("run_id", self.run_id)
        if self.command:
            payload.setdefault("command", self.command)
        level = str(_level or "info").lower()
        log_method = getattr(self._run_logger, level, None)
        if log_method is None:
            log_method = self._run_logger.info
        log_method(event_name, **payload)

    def log_exception_event(  # noqa: PLR0913
        self,
        event_name: str,
        exc: BaseException,
        *,
        _level: str = "error",
        stage: str | None = None,
        task_id: str | None = None,
        recoverable: bool = False,
        cached: bool = False,
        acceptance_phase: bool = False,
        log_path: str | Path | None = None,
        session_path: str | Path | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Record a caught exception in run.log and the central error index."""

        from tend.utils.failures import exception_summary

        payload = exception_summary(exc, stage=stage, task_id=task_id)
        payload.setdefault("error", payload.get("error_message", ""))
        payload.update(
            {
                "recoverable": bool(recoverable),
                "cached": bool(cached),
                "acceptance_phase": bool(acceptance_phase),
            }
        )
        if log_path is not None:
            payload["log_path"] = str(log_path)
        if session_path is not None:
            payload["session_path"] = str(session_path)
        payload.update(kwargs)

        self.log_run_event(event_name, _level=_level, **payload)

        record = dict(payload)
        record["event"] = event_name
        record.setdefault("run_id", self.run_id)
        if self.command:
            record.setdefault("command", self.command)
        record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        with self._error_lock:
            self._error_count += 1
            record["error_index"] = self._error_count
            with open(self._errors_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                f.flush()
        return record

    def _tee_error_record(self, record: logging.LogRecord) -> None:
        """Append an ERROR+ ``LogRecord`` to ``errors.jsonl``.

        Invoked by :class:`_ErrorTeeFilter` for every ``tend.*`` record at
        ``ERROR`` or above that did NOT originate from
        :meth:`log_exception_event` (those already self-index). The record is
        serialised with the shared JSON formatter and stamped with the next
        ``error_index`` under the shared error lock so the index sequence is
        unbroken across both write paths.
        """
        line = _get_json_formatter().format(record)
        try:
            payload = json.loads(line)
        except (ValueError, TypeError):
            payload = {"event": record.getMessage(), "raw": line}
        payload.setdefault("run_id", self.run_id)
        if self.command:
            payload.setdefault("command", self.command)
        with self._error_lock:
            self._error_count += 1
            payload["error_index"] = self._error_count
            with open(self._errors_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
                f.flush()

    def write_run_summary(
        self,
        *,
        outcome: str,
        elapsed_s: float | None = None,
        **kwargs: Any,
    ) -> None:
        """Persist the final run-level diagnostic summary."""

        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "command": self.command,
            "outcome": outcome,
            "started_at": self._started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error_count": self._error_count,
            "run_log": str(self._run_log_path),
            "errors_log": str(self._errors_path),
        }
        if elapsed_s is not None:
            payload["elapsed_s"] = round(float(elapsed_s), 2)
        payload.update(kwargs)
        with open(self._summary_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            f.write("\n")

    def append_cost_record(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, default=str) + "\n"
        last_err: OSError | None = None
        with self._cost_lock:
            for attempt in range(_COST_APPEND_RETRIES):
                try:
                    with open(self._cost_path, "a", encoding="utf-8") as f:
                        f.write(line)
                        f.flush()
                    return
                except PermissionError as err:
                    last_err = err
                    if attempt == _COST_APPEND_RETRIES - 1:
                        break
                    time.sleep(_COST_APPEND_RETRY_BASE_SECONDS * (attempt + 1))
        try:
            self._run_logger.warning(
                "cost_summary_append_failed",
                path=str(self._cost_path),
                error=str(last_err) if last_err is not None else "unknown",
                attempts=_COST_APPEND_RETRIES,
            )
        except Exception:
            pass
