from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog

from tend.utils.logging._config import _sanitize_log_kwargs
from tend.utils.logging._formatters import (
    _append_agent_payload_section,
    _format_agent_messages_md,
    _format_llm_request_as_markdown,
    _format_llm_response_as_markdown,
    _format_logged_cost,
    _format_seed_audit_log,
    _format_seed_outcome_section,
    _format_tool_calls_md,
    _format_tool_results_md,
)
from tend.utils.logging._paths import _generate_call_id, _open_path

if TYPE_CHECKING:
    from tend.utils.logging._log_manager import LogManager


@dataclass(frozen=True, slots=True)
class AgentTurnLogPayload:
    turn: int
    max_turns: int | None = None
    reasoning: str | None = None
    assistant_content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_results: list[dict[str, Any]] | None = None
    usage: dict[str, int] | None = None
    cost_usd: float = 0.0
    cost_source: str | None = None


@dataclass(frozen=True, slots=True)
class ContextSnapshotLogPayload:
    compact_count: int
    turn: int
    trigger: str
    old_message_count: int
    new_message_count: int
    old_token_estimate: int
    new_token_estimate: int
    summary_text: str
    trigger_source: str = "unknown"
    preserved_token_estimate: int | None = None
    pinned_facts: str = ""
    tail_messages: list[dict[str, Any]] | None = None
    compaction_path: str = "normal"


class TaskLogger:
    """Bound to one task's log file. Handles detailed logging and LLM call records.

    Supports two modes, selected automatically:

    - **Agent mode**: When ``open_agent_session`` has been called, all LLM
      interactions are recorded as blockquoted agent I/O payload sections in a
      single session ``.md`` file. ``log_llm_request`` becomes a no-op and
      ``log_llm_response`` only writes cost records.
    - **Workflow mode** (default): Each LLM call produces its own ``.md``
      file via ``log_llm_request`` / ``log_llm_response``.
    """

    def __init__(
        self,
        stage: str,
        task_id: str,
        logger: structlog.stdlib.BoundLogger,
        llm_dir: Path,
        manager: LogManager,
        log_path: Path,
    ) -> None:
        self.stage = stage
        self.task_id = task_id
        self.log_path = log_path
        self._log = logger.bind(
            run_id=manager.run_id,
            command=manager.command,
            stage=stage,
            task_id=task_id,
        )
        self._llm_dir = llm_dir
        self._manager = manager

        self._agent_session_path: Path | None = None
        self._agent_session_active: bool = False
        self._agent_session_cost: float = 0.0
        self._agent_session_cost_sources: set[str] = set()
        self._last_agent_session_path: Path | None = None
        self._step_label: str = ""
        self._last_llm_call_path: Path | None = None

    # --- standard log methods ---

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
            task_id=self.task_id,
            log_path=self.log_path,
            session_path=self._agent_session_path,
            **kw,
        )
        log_method = getattr(self._log, str(_level or "error").lower(), self._log.error)
        log_method(event_name, **_sanitize_log_kwargs(payload))
        return payload

    @property
    def agent_session_path(self) -> Path | None:
        """Path of the active agent session file, or the last closed one."""
        return self._agent_session_path or self._last_agent_session_path

    # --- Step label for LLM log filenames ---

    @property
    def step_label(self) -> str:
        return self._step_label

    def set_step_label(self, label: str) -> None:
        """Set a label that is prepended to LLM log filenames for this task."""
        self._step_label = label

    @property
    def log_root_dir(self) -> Path:
        """Root directory for files this logger should drop alongside its
        per-call ``llm/`` markdown logs.

        The previous shape required callers to duck-type a probe over
        ``log_root`` / ``task_dir`` / ``_llm_dir`` and then walk up if the
        attribute happened to point at the ``llm/`` subdirectory. With
        this property the contract is explicit: it returns the parent of
        ``_llm_dir`` (so e.g. seed-failure JSONL logs can live as
        siblings of the per-call markdown).
        """
        return self._llm_dir.parent

    # --- Agent session logging ---

    def open_agent_session(
        self,
        *,
        model: str,
        system_prompt: str,
        user_message: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        """Create a session ``.md`` file and enter agent mode.

        While agent mode is active, ``log_llm_request`` is a no-op and
        ``log_llm_response`` only records cost.  All substantive logging
        goes through ``log_agent_turn``.

        Best-effort: exceptions are caught so a logging failure never
        prevents the agent from running.
        """
        try:
            self._open_agent_session_inner(
                model=model,
                system_prompt=system_prompt,
                user_message=user_message,
                tools=tools,
            )
        except Exception as exc:
            structlog.get_logger("tend.logging").warning(
                "open_agent_session_failed",
                error=str(exc),
                task_id=self.task_id,
            )

    def _open_agent_session_inner(
        self,
        *,
        model: str,
        system_prompt: str,
        user_message: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        session_id = _generate_call_id(self._step_label)
        if self._agent_session_active and self._agent_session_path is not None:
            structlog.get_logger("tend.logging").warning(
                "agent_session_overlap",
                stage=self.stage,
                task_id=self.task_id,
                existing_session=self._agent_session_path.name,
                new_session=f"{session_id}.md",
                hint="Two concurrent AgentBase calls share the same TaskLogger; "
                "use TaskLogger.create_child(step_label=...) before instantiating AgentBase.",
            )
        md_path = self._llm_dir / f"{session_id}.md"
        ts = datetime.now(timezone.utc).isoformat()

        lines: list[str] = [
            f"# Agent Session: {session_id}",
            "",
            "| Field | Value |",
            "|-------|-------|",
            f"| Stage | {self.stage} |",
            f"| Task | {self.task_id} |",
            f"| Model | {model} |",
            f"| Started | {ts} |",
            "",
        ]

        _append_agent_payload_section(
            lines,
            "## System Prompt",
            system_prompt,
        )
        _append_agent_payload_section(
            lines,
            "## User Message",
            user_message,
        )
        if tools:
            lines += ["## Tools", ""]
            lines += ["```json", json.dumps(tools, indent=2, default=str), "```", ""]

        lines += ["---", ""]

        with open(_open_path(md_path), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        self._agent_session_path = md_path
        self._agent_session_active = True
        self._agent_session_cost = 0.0
        self._agent_session_cost_sources = set()
        self._last_llm_call_path = None
        self._log.debug("agent_session_opened", session_id=session_id)

    def log_agent_turn(self, payload: AgentTurnLogPayload) -> None:
        """Append one turn to the active agent session file.

        Best-effort: exceptions are caught so a formatting or I/O failure
        never kills the branch.
        """
        if self._agent_session_path is None:
            return
        try:
            self._log_agent_turn_inner(payload)
        except Exception as exc:
            structlog.get_logger("tend.logging").warning(
                "log_agent_turn_failed",
                error=str(exc),
                task_id=self.task_id,
                turn=payload.turn,
            )

    def _log_agent_turn_inner(self, payload: AgentTurnLogPayload) -> None:
        self._agent_session_cost += payload.cost_usd
        if payload.cost_source:
            self._agent_session_cost_sources.add(payload.cost_source)
        turn_header = (
            f"## Turn {payload.turn}/{payload.max_turns}"
            if payload.max_turns
            else f"## Turn {payload.turn}"
        )
        lines: list[str] = [turn_header, ""]

        _append_agent_payload_section(lines, "### Reasoning", payload.reasoning)
        _append_agent_payload_section(lines, "### Content", payload.assistant_content)
        if payload.tool_calls:
            lines += ["### Tool Calls", ""]
            lines += _format_tool_calls_md(payload.tool_calls)

        if payload.tool_results:
            lines += ["### Tool Results", ""]
            lines += _format_tool_results_md(
                payload.tool_results,
                tool_calls=payload.tool_calls,
            )

        if payload.usage:
            prompt_t = payload.usage.get("prompt_tokens", 0)
            completion_t = payload.usage.get("completion_tokens", 0)
            lines += [
                "| Metric | Value |",
                "|--------|-------|",
                f"| Prompt Tokens | {prompt_t} |",
                f"| Completion Tokens | {completion_t} |",
                "| Cost (USD) | "
                f"{_format_logged_cost(cost_usd=payload.cost_usd, cost_source=payload.cost_source, prompt_tokens=prompt_t, completion_tokens=completion_t)} |",
                "",
            ]

        lines += ["---", ""]

        session_path = self._agent_session_path
        assert session_path is not None
        with open(session_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def log_context_snapshot(self, payload: ContextSnapshotLogPayload) -> None:
        """Append a context compaction snapshot to the agent session file.

        Best-effort: exceptions are caught so a formatting or I/O failure
        never kills the agent.
        """
        if self._agent_session_path is None:
            return
        try:
            self._log_context_snapshot_inner(payload)
        except Exception as exc:
            structlog.get_logger("tend.logging").warning(
                "log_context_snapshot_failed",
                error=str(exc),
                task_id=self.task_id,
            )

    def _log_context_snapshot_inner(self, payload: ContextSnapshotLogPayload) -> None:
        removed = payload.old_message_count - payload.new_message_count
        reduction = (
            0.0
            if payload.old_token_estimate <= 0
            else (
                payload.old_token_estimate - payload.new_token_estimate
            )
            / payload.old_token_estimate
            * 100
        )
        lines: list[str] = [
            "## Context Compaction "
            f"#{payload.compact_count} (during Turn {payload.turn})",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Source | {payload.trigger_source} |",
            f"| Trigger | {payload.trigger} |",
            f"| Path | {payload.compaction_path} |",
            "| Messages | "
            f"{payload.old_message_count} -> {payload.new_message_count} "
            f"(removed {removed}) |",
            "| Est. Tokens | "
            f"{payload.old_token_estimate} -> {payload.new_token_estimate} |",
            f"| Reduction % | {reduction:.1f}% |",
        ]
        if payload.preserved_token_estimate is not None:
            lines.append(f"| Preserved Tokens | {payload.preserved_token_estimate} |")
        if payload.compaction_path == "ineffective":
            lines.append(
                "| Ineffective Reason | Most remaining tokens are preserved "
                "system/user context, so summarizing turn history cannot "
                "reduce the active context much. |"
            )
        lines.append("")

        _append_agent_payload_section(
            lines,
            "### Compacted Summary",
            payload.summary_text,
        )

        lines += ["---", ""]

        session_path = self._agent_session_path
        assert session_path is not None
        with open(session_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def rotate_agent_session_after_compaction(
        self,
        *,
        compact_count: int,
        turn: int,
        compacted_messages: list[dict[str, Any]],
        summary_text: str,
        compaction_path: str,
        trigger_source: str = "unknown",
    ) -> Path | None:
        """Start a new agent-session markdown segment after compaction.

        The prior session file keeps the full pre-compaction transcript and
        points to the continuation.  The new file starts with the compacted
        active messages rendered as ordinary agent messages, then receives all
        following turns.  Best-effort: failures are logged and return ``None``
        so logging never blocks the agent loop.
        """
        if self._agent_session_path is None or not self._agent_session_active:
            return None
        try:
            return self._rotate_agent_session_after_compaction_inner(
                compact_count=compact_count,
                turn=turn,
                compacted_messages=compacted_messages,
                summary_text=summary_text,
                compaction_path=compaction_path,
                trigger_source=trigger_source,
            )
        except Exception as exc:
            structlog.get_logger("tend.logging").warning(
                "rotate_agent_session_after_compaction_failed",
                error=str(exc),
                task_id=self.task_id,
                compact_count=compact_count,
            )
            return None

    def _rotate_agent_session_after_compaction_inner(
        self,
        *,
        compact_count: int,
        turn: int,
        compacted_messages: list[dict[str, Any]],
        summary_text: str,
        compaction_path: str,
        trigger_source: str = "unknown",
    ) -> Path:
        prior_path = self._agent_session_path
        assert prior_path is not None

        label = f"{self._step_label}_continued" if self._step_label else "continued"
        session_id = _generate_call_id(label)
        next_path = self._llm_dir / f"{session_id}.md"
        ts = datetime.now(timezone.utc).isoformat()

        rel_next = os.path.relpath(next_path, prior_path.parent).replace("\\", "/")
        rel_prior = os.path.relpath(prior_path, next_path.parent).replace("\\", "/")

        continuation = [
            "",
            "## Continued After Context Compaction",
            "",
            "| Field | Value |",
            "|-------|-------|",
            f"| Compaction | #{compact_count} during Turn {turn} |",
            f"| Source | {trigger_source} |",
            f"| Path | {compaction_path} |",
            f"| Next Log | [{next_path.name}]({rel_next}) |",
            "",
            "This is not a crash; the agent log was rotated after context "
            "compaction. Read the latest continuation log for the final "
            "session outcome.",
            "",
            "---",
            "",
        ]
        with open(_open_path(prior_path), "a", encoding="utf-8") as f:
            f.write("\n".join(continuation))

        lines: list[str] = [
            f"# Agent Session Continuation: {session_id}",
            "",
            "| Field | Value |",
            "|-------|-------|",
            f"| Stage | {self.stage} |",
            f"| Task | {self.task_id} |",
            f"| Continued From | [{prior_path.name}]({rel_prior}) |",
            f"| Compaction | #{compact_count} during Turn {turn} |",
            f"| Source | {trigger_source} |",
            f"| Path | {compaction_path} |",
            f"| Started | {ts} |",
            "",
        ]
        lines += ["## Messages", ""]
        lines += _format_agent_messages_md(compacted_messages)
        lines += [
            "---",
            "",
        ]
        with open(_open_path(next_path), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        self._agent_session_path = next_path
        self._agent_session_active = True
        self._log.debug(
            "agent_session_rotated_after_compaction",
            compact_count=compact_count,
            prior_path=str(prior_path),
            next_path=str(next_path),
        )
        return next_path

    def close_agent_session(
        self,
        *,
        turns: int,
        tool_calls_made: int,
        total_tokens: int,
        total_cost: float | None = None,
        total_cost_source: str | None = None,
        completed: bool,
        reason: str | None = None,
        outcome: str | None = None,
    ) -> None:
        """Append a footer and exit agent mode.

        Best-effort: exceptions are caught so a footer write failure
        never propagates into the agent loop.

        ``outcome`` (when provided) is one of
        ``"submitted" | "abandoned" | "interrupted"`` and supersedes the
        legacy ``completed``/``reason`` derivation in the footer status.
        ``completed`` and ``reason`` are still required for backward
        compatibility with call sites that have not yet been migrated.
        """
        if self._agent_session_path is None:
            return
        try:
            self._close_agent_session_inner(
                turns=turns,
                tool_calls_made=tool_calls_made,
                total_tokens=total_tokens,
                total_cost=total_cost,
                total_cost_source=total_cost_source,
                completed=completed,
                reason=reason,
                outcome=outcome,
            )
        except Exception as exc:
            structlog.get_logger("tend.logging").warning(
                "close_agent_session_failed",
                error=str(exc),
                task_id=self.task_id,
            )
        finally:
            self._last_agent_session_path = self._agent_session_path
            self._agent_session_path = None
            self._agent_session_active = False
            self._agent_session_cost = 0.0
            self._agent_session_cost_sources = set()

    def _close_agent_session_inner(
        self,
        *,
        turns: int,
        tool_calls_made: int,
        total_tokens: int,
        total_cost: float | None = None,
        total_cost_source: str | None = None,
        completed: bool,
        reason: str | None = None,
        outcome: str | None = None,
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        if outcome is not None:
            # Layer 6: render the strict outcome enumeration verbatim so
            # postmortems and dashboards can rely on the literal token.
            # Recognised tokens:
            #   submitted_clean, submitted_with_warnings,
            #   rejected_validation, rejected_max_retries,
            #   rejected_thrash, aborted_scope_inadequate,
            #   aborted_no_submission, abandoned, submitted, interrupted
            if outcome == "abandoned":
                status = f"abandoned ({reason})" if reason else "abandoned"
            elif outcome in ("submitted", "submitted_clean", "submitted_with_warnings"):
                status = outcome
            elif outcome.startswith("rejected_") or outcome.startswith("aborted_"):
                status = f"{outcome} ({reason})" if reason else outcome
            else:
                status = f"interrupted ({reason})" if reason else "interrupted"
        else:
            status = "completed" if completed else f"interrupted ({reason})"
        resolved_cost = (
            total_cost if total_cost is not None else self._agent_session_cost
        )
        if total_cost_source is None:
            seen: set[str] = getattr(self, "_agent_session_cost_sources", set())
            if "api" in seen:
                total_cost_source = "api"
            elif seen:
                total_cost_source = "unavailable"
        cost_label = _format_logged_cost(
            cost_usd=resolved_cost,
            cost_source=total_cost_source,
            prompt_tokens=total_tokens,
        )
        footer = (
            "\n## Session Complete\n\n"
            "| Field | Value |\n"
            "|-------|-------|\n"
            f"| Finished | {ts} |\n"
            f"| Outcome | {status} |\n"
            f"| Turns | {turns} |\n"
            f"| Tool Calls | {tool_calls_made} |\n"
            f"| Total Tokens | {total_tokens} |\n"
            f"| Total Cost (USD) | {cost_label} |\n"
        )
        session_path = self._agent_session_path
        assert session_path is not None
        with open(session_path, "a", encoding="utf-8") as f:
            f.write(footer)

        self._log.debug(
            "agent_session_closed",
            turns=turns,
            completed=completed,
            outcome=outcome,
        )

    # --- Postmortem appending (after session is closed) ---

    def append_postmortem_section(
        self,
        session_log_path: Path,
        postmortem_dict: dict[str, Any],
    ) -> None:
        """Append a ``## Postmortem`` block to a closed agent session log.

        Best-effort: silently logs a warning on failure so that a missing
        log file or filesystem error never blocks the pipeline.
        """
        try:
            section = self._render_postmortem_md(postmortem_dict)
            with open(session_log_path, "a", encoding="utf-8") as f:
                f.write(section)
        except Exception as exc:
            self._log.warning(
                "append_postmortem_failed",
                path=str(session_log_path),
                error=str(exc),
            )

    @staticmethod
    def _render_postmortem_md(pm: dict[str, Any]) -> str:
        evidence = pm.get("evidence") or []
        attempts = pm.get("attempted_approaches") or []
        evidence_md = (
            "\n".join(f"  - {e}" for e in evidence) if evidence else "  - (none)"
        )
        attempts_md = (
            "\n".join(f"  - {a}" for a in attempts) if attempts else "  - (none)"
        )
        confidence = pm.get("confidence", 0.0)
        try:
            confidence_str = f"{float(confidence):.2f}"
        except (TypeError, ValueError):
            confidence_str = str(confidence)
        return (
            "\n\n## Postmortem\n\n"
            f"- **Phase**: {pm.get('phase', '-')}\n"
            f"- **Root Cause**: {pm.get('root_cause_category', '-')} "
            f"(confidence {confidence_str})\n"
            f"- **Recommended Action**: {pm.get('recommended_next_action', '-')}\n"
            f"- **Summary**: {pm.get('root_cause_summary', '-')}\n"
            f"- **Evidence**:\n{evidence_md}\n"
            f"- **Attempted Approaches**:\n{attempts_md}\n"
        )

    # --- Agent session suspend / resume (for nested child agents) ---

    def suspend_agent_session(self) -> tuple[Path | None, bool, float]:
        """Snapshot current session state for safe nesting by a child agent."""
        return (
            self._agent_session_path,
            self._agent_session_active,
            self._agent_session_cost,
        )

    def resume_agent_session(
        self,
        snapshot: tuple[Path | None, bool, float],
    ) -> None:
        """Restore session state from a previous suspend call."""
        self._agent_session_path = snapshot[0]
        self._agent_session_active = snapshot[1]
        self._agent_session_cost = snapshot[2]

    def create_child(self, *, step_label: str = "") -> TaskLogger:
        """Return a new TaskLogger sharing ``_llm_dir`` but with independent session state.

        Useful for running parallel agents that each need their own log
        file without interfering with this logger's session.
        """
        child = TaskLogger(
            stage=self.stage,
            task_id=self.task_id,
            logger=self._log,
            llm_dir=self._llm_dir,
            manager=self._manager,
            log_path=self.log_path,
        )
        if step_label:
            child.set_step_label(step_label)
        return child

    # --- LLM call logging (session-aware) ---

    def new_llm_call_id(self) -> str:
        return _generate_call_id(self._step_label)

    def log_llm_request(
        self,
        call_id: str,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        """Write LLM request to ``llm/{call_id}.md``. No-op during agent sessions."""
        if self._agent_session_active:
            return

        record: dict[str, Any] = {
            "type": "request",
            "call_id": call_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
            "response_format": response_format,
            "stage": self.stage,
            "task_id": self.task_id,
            **extra,
        }
        md_path = self._llm_dir / f"{call_id}.md"
        with open(_open_path(md_path), "w", encoding="utf-8") as f:
            f.write(_format_llm_request_as_markdown(record))
        self._last_llm_call_path = md_path
        self._last_agent_session_path = None
        self._log.debug("llm_request_logged", call_id=call_id, model=model)

    def append_llm_outcome(
        self,
        *,
        title: str,
        fields: dict[str, Any] | None = None,
        payload: Any | None = None,
    ) -> None:
        """Append local post-processing outcome to the most recent LLM call log.

        This is for workflow-mode calls where Python validates/applies an LLM
        draft after the provider response has already been logged.
        """
        if self._agent_session_active:
            return
        outcome_path = self._last_llm_call_path or self._last_agent_session_path
        if outcome_path is None:
            return
        try:
            self._append_llm_outcome_inner(
                path=outcome_path,
                title=title,
                fields=fields or {},
                payload=payload,
            )
        except Exception as exc:
            structlog.get_logger("tend.logging").warning(
                "append_llm_outcome_failed",
                error=str(exc),
                task_id=self.task_id,
            )

    def append_llm_outcome_for_call(
        self,
        call_id: str,
        *,
        title: str,
        fields: dict[str, Any] | None = None,
        payload: Any | None = None,
    ) -> None:
        """Append an outcome to a specific LLM call log or active agent session."""
        outcome_path: Path | None
        if self._agent_session_active:
            outcome_path = self._agent_session_path
        else:
            outcome_path = self._llm_dir / f"{call_id}.md"
        if outcome_path is None:
            return
        try:
            self._append_llm_outcome_inner(
                path=outcome_path,
                title=title,
                fields=fields or {},
                payload=payload,
            )
        except Exception as exc:
            structlog.get_logger("tend.logging").warning(
                "append_llm_outcome_for_call_failed",
                call_id=call_id,
                error=str(exc),
                task_id=self.task_id,
            )

    def _append_llm_outcome_inner(
        self,
        *,
        path: Path,
        title: str,
        fields: dict[str, Any],
        payload: Any | None,
    ) -> None:
        lines: list[str] = ["", f"## {title}", ""]
        if fields:
            lines += ["| Field | Value |", "|-------|-------|"]
            for key, value in fields.items():
                lines.append(f"| {key} | {value} |")
            lines.append("")
        if payload is not None:
            lines += [
                "### Details",
                "",
                "```json",
                json.dumps(payload, indent=2, default=str),
                "```",
                "",
            ]
        with open(_open_path(path), "a", encoding="utf-8") as f:
            f.write("\n".join(lines))

    @property
    def last_llm_call_path(self) -> Path | None:
        """Path of the most recent ``llm/{call_id}.md`` written by this logger."""

        return self._last_llm_call_path

    def append_seed_outcome(
        self,
        *,
        decision: Literal["accepted", "rejected", "dropped"],
        reason: str,
        artifact_kind: Literal["atom", "scenario", "window"],
        **fields: Any,
    ) -> None:
        """Append a ``## Seed Phase Outcome`` section to the LAST LLM call log.

        Pairs with the existing ``## Structured Parse Outcome`` convention
        so a single log file tells the reader whether the seed-phase
        artifact was accepted, rejected, or dropped — and why.  Silently
        no-ops when no LLM call has been made yet (so deterministic
        rejections that happen before any LLM call do not crash).
        ``fields`` appears as additional rows in the rendered table.
        """

        if self._last_llm_call_path is None:
            return
        section = _format_seed_outcome_section(
            decision=decision,
            reason=reason,
            artifact_kind=artifact_kind,
            fields=fields,
        )
        with open(_open_path(self._last_llm_call_path), "a", encoding="utf-8") as f:
            f.write(section)

    def write_seed_audit_log(
        self,
        *,
        filename: str,
        title: str,
        records: list[dict[str, Any]],
    ) -> Path | None:
        """Write a free-standing seed-phase summary log under ``llm/``.

        Used for deterministic decisions (scenarios) and skipped-before-LLM
        cases (single-table window skips) where no per-call markdown file
        exists.  Each record dict becomes one row in a rendered markdown
        table.  Returns the written path (or ``None`` if called during an
        agent session).
        """

        if self._agent_session_active:
            return None
        md_path = self._llm_dir / filename
        md_path.parent.mkdir(parents=True, exist_ok=True)
        with open(_open_path(md_path), "w", encoding="utf-8") as f:
            f.write(_format_seed_audit_log(title, records))
        return md_path

    def log_llm_response(
        self,
        call_id: str,
        *,
        response_raw: Any,
        usage: dict[str, int],
        finish_reason: str,
        cost_usd: float,
        cost_source: str | None = None,
    ) -> None:
        """Append response to ``llm/{call_id}.md``.

        During agent sessions, only records cost to ``cost_summary.jsonl``.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        raw = response_raw if isinstance(response_raw, dict) else {}

        self._manager.append_cost_record(
            {
                "call_id": call_id,
                "timestamp": timestamp,
                "model": raw.get("model", "unknown"),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "cache_hit_tokens": usage.get("cache_hit_tokens", 0),
                "cache_miss_tokens": usage.get("cache_miss_tokens", 0),
                "cost_usd": cost_usd,
                "cost_source": cost_source or "api",
                "stage": self.stage,
                "task_id": self.task_id,
            }
        )

        if self._agent_session_active:
            self._log.debug(
                "llm_response_cost_only",
                call_id=call_id,
                cost_usd=cost_usd,
            )
            return

        md_path = self._llm_dir / f"{call_id}.md"
        with open(_open_path(md_path), "a", encoding="utf-8") as f:
            f.write(
                _format_llm_response_as_markdown(
                    response_raw=response_raw,
                    usage=usage,
                    finish_reason=finish_reason,
                    cost_usd=cost_usd,
                    cost_source=cost_source,
                    timestamp=timestamp,
                )
            )

        self._log.debug(
            "llm_response_logged",
            call_id=call_id,
            cost_usd=cost_usd,
            tokens=usage,
        )
