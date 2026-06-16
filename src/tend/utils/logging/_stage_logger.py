from __future__ import annotations

# ruff: noqa: F403,F405

from tend.utils.logging._config import *

class StageLogger:
    """Bound to one stage's log file. Records lifecycle events only."""

    def __init__(
        self,
        stage: str,
        logger: structlog.stdlib.BoundLogger,
        manager: LogManager,
        log_path: Path,
    ) -> None:
        self.stage = stage
        self.log_path = log_path
        self._log = logger.bind(
            run_id=manager.run_id,
            command=manager.command,
            stage=stage,
        )
        self._manager = manager

    def info(self, event_name: str, **kw: Any) -> None:
        self._log.info(event_name, **_sanitize_log_kwargs(kw))

    def warning(self, event_name: str, **kw: Any) -> None:
        self._log.warning(event_name, **_sanitize_log_kwargs(kw))

    def error(self, event_name: str, **kw: Any) -> None:
        self._log.error(event_name, **_sanitize_log_kwargs(kw))

    def debug(self, event_name: str, **kw: Any) -> None:
        self._log.debug(event_name, **_sanitize_log_kwargs(kw))

    def critical(self, event_name: str, **kw: Any) -> None:
        self._log.critical(event_name, **_sanitize_log_kwargs(kw))

    def exception(
        self,
        event_name: str,
        exc: BaseException,
        *,
        _level: str = "error",
        **kw: Any,
    ) -> dict[str, Any]:
        payload = self._manager.log_exception_event(
            event_name,
            exc,
            _level=_level,
            stage=self.stage,
            log_path=self.log_path,
            **kw,
        )
        log_method = getattr(self._log, str(_level or "error").lower(), self._log.error)
        log_method(event_name, **_sanitize_log_kwargs(payload))
        return payload
