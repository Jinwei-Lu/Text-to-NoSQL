from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

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


_RESPONSE_ANOMALY_SCHEMA = "tend.provider_response_anomaly.v1"
_RESPONSE_ANOMALY_PREVIEW_CHARS = 1024
_RESPONSE_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(
        r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|auth(?:orization)?|cookie|"
        r"password|passwd|secret)[\"']?\s*[:=]\s*)([\"'])([^\"'\r\n]*)([\"'])"
    ),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth(?:orization)?|cookie|"
        r"password|passwd|secret)\b\s*[:=]\s*)([^\s,;}\]]+)"
    ),
    re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@"),
)


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


def _redact_response_preview(text: str) -> str:
    """Redact credential-shaped values from a bounded response excerpt."""

    redacted = _RESPONSE_SECRET_PATTERNS[0].sub("Bearer [REDACTED]", text)
    redacted = _RESPONSE_SECRET_PATTERNS[1].sub("[REDACTED]", redacted)
    redacted = _RESPONSE_SECRET_PATTERNS[2].sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]{match.group(4)}",
        redacted,
    )
    redacted = _RESPONSE_SECRET_PATTERNS[3].sub(
        lambda match: f"{match.group(1)}[REDACTED]",
        redacted,
    )
    return _RESPONSE_SECRET_PATTERNS[4].sub(
        lambda match: f"{match.group(1)}[REDACTED]@",
        redacted,
    )


def _safe_response_evidence_component(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "_"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:80]}-{digest}"


def _atomic_private_json_write(path: Path, payload: dict[str, Any]) -> str:
    """Atomically write canonical JSON with owner-only file permissions."""

    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            _open_path(temporary),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(_open_path(temporary), _open_path(path))
        os.chmod(_open_path(path), 0o600)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
    return hashlib.sha256(encoded).hexdigest()


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
            else (payload.old_token_estimate - payload.new_token_estimate)
            / payload.old_token_estimate
            * 100
        )
        lines: list[str] = [
            f"## Context Compaction #{payload.compact_count} (during Turn {payload.turn})",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Source | {payload.trigger_source} |",
            f"| Trigger | {payload.trigger} |",
            f"| Path | {payload.compaction_path} |",
            "| Messages | "
            f"{payload.old_message_count} -> {payload.new_message_count} "
            f"(removed {removed}) |",
            f"| Est. Tokens | {payload.old_token_estimate} -> {payload.new_token_estimate} |",
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
            elif outcome in ("budget_exhausted", "error"):
                status = outcome
            elif outcome.startswith("rejected_") or outcome.startswith("aborted_"):
                status = f"{outcome} ({reason})" if reason else outcome
            else:
                status = f"interrupted ({reason})" if reason else "interrupted"
        else:
            status = "completed" if completed else f"interrupted ({reason})"
        resolved_cost = total_cost if total_cost is not None else self._agent_session_cost
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
        evidence_md = "\n".join(f"  - {e}" for e in evidence) if evidence else "  - (none)"
        attempts_md = "\n".join(f"  - {a}" for a in attempts) if attempts else "  - (none)"
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
        append_cost_record: bool = True,
    ) -> None:
        """Append response to ``llm/{call_id}.md``.

        During agent sessions, only records cost to ``cost_summary.jsonl``.

        ``append_cost_record=False`` is used by the LLM client's logical-call
        footer after every provider request has already been written as a
        ``provider_attempt`` row.  Keeping the human-readable response/footer
        separate from the billing ledger prevents the final response from being
        charged twice.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        raw = response_raw if isinstance(response_raw, dict) else {}

        if append_cost_record:
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
                    "call_status": raw.get("call_status", "unknown"),
                    "attempt_count": raw.get("attempt_count"),
                    "error": raw.get("error"),
                    # This is the durable per-call routing receipt for OpenRouter runs.
                    # Agent-session calls intentionally do not write a separate response
                    # markdown file, so cost_summary.jsonl must retain the selected
                    # provider, model revision, router attempt count, and generation id.
                    "provider_metadata": raw.get("provider_metadata"),
                    # Preserve the effective wire-level request budget/configuration. This
                    # distinguishes method-native omitted max_tokens from a campaign-enforced
                    # common cap without reconstructing it from source after the fact.
                    "request_config": raw.get("request_config"),
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

    def write_llm_response_anomaly_evidence(
        self,
        call_id: str,
        *,
        provider_attempt_index: int,
        transport_attempt: int,
        repair_index: int,
        response_text: str,
        call_status: str,
        failure_phase: str,
        anomaly: str | None,
        error_type: str | None,
        finish_reason: str | None,
    ) -> dict[str, Any]:
        """Persist bounded evidence for a received response that was rejected.

        The complete response is never copied into this artifact.  Its exact
        UTF-8 digest and sizes make later byte-for-byte comparison possible,
        while the only human-readable material is an owner-readable, bounded,
        credential-redacted head/tail excerpt.
        """

        response_bytes = response_text.encode("utf-8")
        response_chars = len(response_text)
        limit = _RESPONSE_ANOMALY_PREVIEW_CHARS
        if response_chars <= limit:
            raw_head = response_text
            raw_tail = ""
        elif response_chars <= limit * 2:
            raw_head = response_text[:limit]
            raw_tail = response_text[limit:]
        else:
            raw_head = response_text[:limit]
            raw_tail = response_text[-limit:]
        head = _redact_response_preview(raw_head)[:limit]
        tail = _redact_response_preview(raw_tail)[-limit:]

        evidence_dir = self._llm_dir / "provider_response_anomalies"
        manager_root = self._manager.root.resolve()
        llm_root = self._llm_dir.resolve()
        if not llm_root.is_relative_to(manager_root):
            raise ValueError("LLM evidence directory escapes the run root")
        if evidence_dir.exists() and not evidence_dir.resolve().is_relative_to(manager_root):
            raise ValueError("provider response evidence directory escapes the run root")
        evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(_open_path(evidence_dir), 0o700)

        safe_call_id = _safe_response_evidence_component(call_id)
        path = evidence_dir / (
            f"{safe_call_id}.provider-attempt-{int(provider_attempt_index):04d}.json"
        )
        payload = {
            "schema": _RESPONSE_ANOMALY_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "call_id": call_id,
            "provider_attempt_index": int(provider_attempt_index),
            "transport_attempt": int(transport_attempt),
            "repair_index": int(repair_index),
            "call_status": call_status,
            "failure_phase": failure_phase,
            "anomaly": anomaly,
            "error_type": error_type,
            "finish_reason": finish_reason,
            "response_encoding": "utf-8",
            "response_utf8_sha256": hashlib.sha256(response_bytes).hexdigest(),
            "response_utf8_bytes": len(response_bytes),
            "response_chars": response_chars,
            "preview_limit_chars_per_edge": limit,
            "preview_middle_omitted": response_chars > limit * 2,
            "preview_redaction": "credential-patterns-v1",
            "preview_redaction_applied": head != raw_head or tail != raw_tail,
            "response_head": head,
            "response_tail": tail,
        }
        sidecar_sha256 = _atomic_private_json_write(path, payload)
        return {
            "schema": _RESPONSE_ANOMALY_SCHEMA,
            "path": path.relative_to(self._manager.root).as_posix(),
            "sidecar_sha256": sidecar_sha256,
            "response_utf8_sha256": payload["response_utf8_sha256"],
            "response_utf8_bytes": payload["response_utf8_bytes"],
            "response_chars": payload["response_chars"],
        }

    def log_llm_attempt(
        self,
        call_id: str,
        *,
        agent: str,
        model: str,
        provider_attempt_index: int,
        transport_attempt: int,
        repair_index: int,
        call_status: str,
        response_received: bool,
        usage: dict[str, Any] | None,
        finish_reason: str | None,
        cost_usd: float | None,
        cost_source: str,
        provider_metadata: dict[str, Any] | None,
        request_config: dict[str, Any],
        retry_kind: str | None = None,
        anomaly: str | None = None,
        error: dict[str, Any] | None = None,
        response_anomaly_evidence: dict[str, Any] | None = None,
    ) -> None:
        """Durably append one provider-request attempt to the campaign ledger.

        There is exactly one row per request issued by :meth:`LLMClient.complete`.
        A response-less transport failure is still a row, with ``cost_usd=None``
        and ``cost_source='unknown'``; zero would incorrectly assert that the
        provider did not bill it.  Rows are flushed by ``append_cost_record`` as
        soon as an attempt settles, before retry sleeps or logical-call cleanup.
        """

        resolved_usage = usage if isinstance(usage, dict) else {}
        self._manager.append_cost_record(
            {
                "record_type": "provider_attempt",
                "call_id": call_id,
                "agent": agent,
                "provider_attempt_index": int(provider_attempt_index),
                "transport_attempt": int(transport_attempt),
                "repair_index": int(repair_index),
                "request_kind": "initial" if repair_index == 0 else "json_repair",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "prompt_tokens": resolved_usage.get("prompt_tokens", 0),
                "completion_tokens": resolved_usage.get("completion_tokens", 0),
                "total_tokens": resolved_usage.get(
                    "total_tokens",
                    int(resolved_usage.get("prompt_tokens", 0) or 0)
                    + int(resolved_usage.get("completion_tokens", 0) or 0),
                ),
                "cache_hit_tokens": resolved_usage.get("cache_hit_tokens", 0),
                "cache_miss_tokens": resolved_usage.get("cache_miss_tokens", 0),
                "cost_usd": cost_usd,
                "cost_source": cost_source,
                "stage": self.stage,
                "task_id": self.task_id,
                "call_status": call_status,
                "response_received": bool(response_received),
                "finish_reason": finish_reason,
                "retry_kind": retry_kind,
                "anomaly": anomaly,
                "error": error,
                "response_anomaly_evidence": response_anomaly_evidence,
                "provider_metadata": provider_metadata,
                "request_config": request_config,
                "campaign_profile_name": os.environ.get("TEND_CAMPAIGN_PROFILE_NAME"),
                "campaign_profile_sha256": os.environ.get("TEND_CAMPAIGN_PROFILE_SHA256"),
            }
        )
