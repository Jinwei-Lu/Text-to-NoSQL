"""SMART-EG file artifacts."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


class SmartEGObserver:
    """Append-only artifact writer for one SMART-EG agent session."""

    def __init__(self, run_dir: Path, *, session_id: str | None = None) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or f"smart-eg-{uuid4().hex[:8]}"
        self.agent_dir = self.run_dir / "agent"
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self.agent_jsonl = self.agent_dir / f"{self.session_id}.jsonl"
        self.agent_md = self.agent_dir / f"{self.session_id}.md"
        self.evidence_path = self.run_dir / "evidence_ledger.jsonl"
        self.submit_gates_path = self.run_dir / "submit_gates.jsonl"
        self.cost_path = self.run_dir / "cost_summary.jsonl"
        self.errors_path = self.run_dir / "errors.jsonl"
        self.execution_trace_path = self.run_dir / "execution_trace.jsonl"
        self.progress_path = self.run_dir / "progress.jsonl"
        self._agent_events: list[dict[str, Any]] = []
        self._line_counts: dict[Path, int] = {}

    def agent_ref(self) -> str:
        return f"agent/{self.session_id}.md"

    def agent_jsonl_ref(self, line_no: int | None = None) -> str:
        ref = f"agent/{self.session_id}.jsonl"
        return f"{ref}#{line_no}" if line_no is not None else ref

    def evidence_ref(self) -> str:
        return "evidence_ledger.jsonl"

    def execution_trace_ref(self) -> str:
        return "execution_trace.jsonl"

    def agent_event(self, event: str, payload: dict[str, Any] | None = None) -> str:
        record = {"ts": _utcnow(), "event": event, **dict(payload or {})}
        line_no = self._append_jsonl(self.agent_jsonl, record)
        self._agent_events.append(record)
        return self.agent_jsonl_ref(line_no)

    def record_evidence(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), "event": "evidence_recorded", **dict(payload)}
        line_no = self._append_jsonl(self.evidence_path, record)
        self.agent_event("evidence_added", {"evidence_id": record.get("evidence_id")})
        return f"evidence_ledger.jsonl#{line_no}"

    def record_submit_gate(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), **dict(payload)}
        line_no = self._append_jsonl(self.submit_gates_path, record)
        ref = f"submit_gates.jsonl#{line_no}"
        self.agent_event(
            "submit_gate_checked",
            {
                "submit_tool": record.get("submit_tool"),
                "accepted": record.get("accepted"),
                "gate_ref": ref,
            },
        )
        return ref

    def record_cost(self, payload: dict[str, Any]) -> str:
        record = {
            "ts": _utcnow(),
            "cost_source": "unavailable",
            **dict(payload),
        }
        line_no = self._append_jsonl(self.cost_path, record)
        return f"cost_summary.jsonl#{line_no}"

    def record_error(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), **dict(payload)}
        line_no = self._append_jsonl(self.errors_path, record)
        return f"errors.jsonl#{line_no}"

    def record_execution_trace(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), **dict(payload)}
        line_no = self._append_jsonl(self.execution_trace_path, record)
        return f"execution_trace.jsonl#{line_no}"

    def record_progress(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), "solver_id": "smart-eg", **dict(payload)}
        line_no = self._append_jsonl(self.progress_path, record)
        return f"progress.jsonl#{line_no}"

    def finalize_markdown(self, *, final_status: str, state_summary: dict[str, Any]) -> None:
        lines = [
            f"# SMART-EG Agent Session: {self.session_id}",
            "",
            f"- Final status: {final_status}",
            f"- Generated: {_utcnow()}",
            "",
            "## State Summary",
            "",
            "```json",
            json.dumps(state_summary, indent=2, ensure_ascii=False, default=str),
            "```",
            "",
            "## Event Tail",
            "",
        ]
        for event in self._agent_events[-80:]:
            lines.append(
                f"- {event.get('ts')} `{event.get('event')}` "
                f"{json.dumps(event, ensure_ascii=False, default=str)}"
            )
        lines.append("")
        self.agent_md.write_text("\n".join(lines), encoding="utf-8")

    def close(self) -> None:
        return None

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        safe = {key: _json_safe(value) for key, value in record.items()}
        previous = self._line_counts.get(path)
        if previous is None:
            previous = self._count_existing_lines(path)
        line_no = previous + 1
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(safe, ensure_ascii=False, default=str) + "\n")
        self._line_counts[path] = line_no
        return line_no

    @staticmethod
    def _count_existing_lines(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(1 for _line in path.read_text(encoding="utf-8").splitlines())


class SmartEGRecorder(SmartEGObserver):
    """Runtime-facing alias around :class:`SmartEGObserver`.

    The recorder accepts a RunLogger so callers do not have to reach into logger internals
    for the run directory.
    """

    def __init__(self, logger: Any, *, session_id: str | None = None) -> None:
        run_dir = getattr(logger, "run_dir", logger)
        super().__init__(Path(run_dir), session_id=session_id)

    def agent_event(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
        **fields: Any,
    ) -> str:  # type: ignore[override]
        return super().agent_event(event, {**dict(payload or {}), **fields})

    def write_evidence(self, payload: dict[str, Any]) -> str:
        self.record_evidence(payload)
        return self.evidence_ref()

    def write_submit_gate(self, payload: dict[str, Any]) -> str:
        self.record_submit_gate(payload)
        return "submit_gates.jsonl"

    def write_cost_summary(self, payload: dict[str, Any]) -> str:
        self.record_cost(payload)
        return "cost_summary.jsonl"

    def write_error(self, payload: dict[str, Any]) -> str:
        self.record_error(payload)
        return "errors.jsonl"

    def final_markdown(self, text: str) -> None:
        self.finalize_markdown(final_status=text, state_summary={})
