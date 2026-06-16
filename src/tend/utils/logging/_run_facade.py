"""RunLogger-shaped façade over :class:`LogManager` for migrated CLI paths.

The solve/baseline/ablation runtimes log exclusively through the DynaDB-style
``LogManager``/``StageLogger``/``TaskLogger`` stack. A handful of shared CLI
helpers and constructors (progress reporter, LLM/Mongo clients, run
finalization) still speak the legacy ``RunLogger`` surface
(``bind``/``info``/``anomaly``/``finalizer().finish``/``close``). This façade
adapts that surface onto the manager so every artifact lands in DynaDB format:
events go to ``run.log``/``milestones.jsonl`` via ``log_run_event``, anomalies
go to ``errors.jsonl`` via ``log_exception_event``, and the final summary goes
to ``run_summary.json`` via ``write_run_summary``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from tend.utils.logging._log_manager import LogManager

AnomalyCallback = Callable[[dict[str, Any]], None]
EventCallback = Callable[[dict[str, Any]], None]


class _Finalizer:
    def __init__(self, facade: "RunLoggerFacade") -> None:
        self._facade = facade

    def finish(self, **fields: Any) -> None:
        status = str(fields.pop("status", "unknown") or "unknown")
        self._facade.manager.write_run_summary(outcome=status, **fields)
        self._facade._shared["summary_written"] = True


class RunLoggerFacade:
    """Adapter exposing the legacy run-logger surface over a ``LogManager``."""

    def __init__(
        self,
        manager: LogManager,
        *,
        context: dict[str, Any] | None = None,
        _shared: dict[str, Any] | None = None,
    ) -> None:
        self.manager = manager
        self._context = dict(context or {})
        # Shared across bind() children: subscriber lists + summary flag.
        self._shared = _shared if _shared is not None else {
            "anomaly_subs": [],
            "event_subs": [],
            "anomaly_counts": {},
            "summary_written": False,
        }

    # ------------------------------------------------------------------ #
    @property
    def run_dir(self) -> Path:
        return self.manager.root

    def bind(self, **fields: Any) -> "RunLoggerFacade":
        return RunLoggerFacade(
            self.manager,
            context={**self._context, **fields},
            _shared=self._shared,
        )

    def create_child(self, **fields: Any) -> "RunLoggerFacade":
        return self.bind(**fields)

    # ------------------------------------------------------------------ #
    def _emit(self, level: str, event: str, fields: dict[str, Any]) -> None:
        payload = {**self._context, **fields}
        self.manager.log_run_event(event, _level=level, **payload)
        record = {"event": event, "level": level, **payload}
        for cb in self._shared["event_subs"]:
            try:
                cb(record)
            except Exception:
                pass

    def debug(self, event: str, **fields: Any) -> None:
        self._emit("debug", event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit("info", event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit("warning", event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit("error", event, fields)

    # ------------------------------------------------------------------ #
    def exception(self, event: str, exc: BaseException, **fields: Any) -> dict[str, Any]:
        merged = {**self._context, **fields}
        stage = merged.pop("stage", None)
        task_id = merged.pop("task_id", None)
        return self.manager.log_exception_event(
            event, exc, stage=stage, task_id=task_id, **merged
        )

    def anomaly(
        self,
        err: BaseException | None = None,
        *,
        kind: Any = None,
        message: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        merged = {**self._context, **fields}
        anomaly_value = getattr(getattr(err, "anomaly", None), "value", None)
        if anomaly_value is None and kind is not None:
            anomaly_value = getattr(kind, "value", None) or str(kind)
        err_context = getattr(err, "context", None)
        if isinstance(err_context, dict):
            for key, value in err_context.items():
                merged.setdefault(key, value)
        stage = merged.pop("stage", None)
        task_id = merged.pop("task_id", None)
        if err is None:
            err = RuntimeError(message or anomaly_value or "anomaly")
        event = str(merged.pop("event", "anomaly_recorded"))
        if anomaly_value is not None:
            merged.setdefault("anomaly", anomaly_value)
        if message is not None:
            merged.setdefault("message", message)
        record = self.manager.log_exception_event(
            event, err, stage=stage, task_id=task_id, **merged
        )
        if getattr(err, "logged", None) is False:
            try:
                err.logged = True  # type: ignore[attr-defined]
            except Exception:
                pass
        counts = self._shared["anomaly_counts"]
        key = str(merged.get("anomaly", "internal"))
        counts[key] = counts.get(key, 0) + 1
        for cb in self._shared["anomaly_subs"]:
            try:
                cb(record)
            except Exception:
                pass
        return record

    def record_error(self, event: str = "error_recorded", **fields: Any) -> dict[str, Any]:
        merged = {**self._context, **fields}
        stage = merged.pop("stage", None)
        task_id = merged.pop("task_id", None)
        return self.manager.log_exception_event(
            event,
            RuntimeError(str(merged.get("message", event))),
            stage=stage,
            task_id=task_id,
            **merged,
        )

    # ------------------------------------------------------------------ #
    def _task_logger_for(self, agent: str) -> Any:
        cache = self._shared.setdefault("agent_task_loggers", {})
        if agent not in cache:
            stage = self.manager.command or "run"
            cache[agent] = self.manager.get_task_logger(stage, agent)
        return cache[agent]

    def save_transcript(self, agent: str, call_id: str, transcript: dict[str, Any]) -> str:
        """Legacy transcript hook: route into DynaDB workflow-mode call logs."""
        try:
            task_log = self._task_logger_for(agent)
            if transcript.get("started"):
                task_log.log_llm_request(
                    call_id,
                    model=str(transcript.get("model", "")),
                    messages=list(transcript.get("messages", []) or []),
                    tools=transcript.get("tools"),
                    temperature=transcript.get("temperature"),
                    response_format=transcript.get("response_format"),
                )
            else:
                usage = dict(transcript.get("usage", {}) or {})
                message = transcript.get("assistant_message") or {
                    "role": "assistant",
                    "content": transcript.get("response_text", ""),
                }
                task_log.log_llm_response(
                    call_id,
                    response_raw={
                        "model": transcript.get("model", ""),
                        "choices": [{
                            "message": message,
                            "finish_reason": transcript.get("finish_reason"),
                        }],
                        "usage": usage,
                        "provider_metadata": transcript.get("provider_metadata"),
                    },
                    usage=usage,
                    finish_reason=str(transcript.get("finish_reason") or "unknown"),
                    cost_usd=float(transcript.get("cost_usd", 0.0) or 0.0),
                    cost_source=transcript.get("cost_source"),
                )
            path = getattr(task_log, "last_llm_call_path", None)
            return str(path) if path else ""
        except Exception:
            return ""

    def record_llm_cost(self, **fields: Any) -> dict[str, Any]:
        from datetime import datetime, timezone

        usage = dict(fields.pop("usage", {}) or {})
        row = {
            "call_id": fields.pop("call_id", None),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": fields.pop("model", None),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cost_usd": fields.pop("cost_usd", 0.0) or 0.0,
            "cost_source": fields.pop("cost_source", None) or "unavailable",
            "agent": fields.pop("agent", None),
        }
        row.update({k: v for k, v in fields.items() if v is not None})
        self.manager.append_cost_record(row)
        return row

    # ------------------------------------------------------------------ #
    def subscribe_anomaly(self, callback: AnomalyCallback) -> None:
        self._shared["anomaly_subs"].append(callback)

    def subscribe_event(self, callback: EventCallback) -> None:
        self._shared["event_subs"].append(callback)

    def anomaly_counts(self) -> dict[str, int]:
        return dict(self._shared["anomaly_counts"])

    # ------------------------------------------------------------------ #
    def finalizer(self) -> _Finalizer:
        return _Finalizer(self)

    def close(self) -> None:
        if not self._shared.get("summary_written"):
            self.manager.write_run_summary(outcome="unknown", close_reason="unclosed")
        self.manager.close()
