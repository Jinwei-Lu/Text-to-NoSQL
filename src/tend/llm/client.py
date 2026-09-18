"""OpenAI-compatible async LLM client with transcripts, retries, and anomaly typing.

Responsibilities (and *only* these — agent semantics live in tend/agents):
  1. Send chat completions to the configured provider (DeepSeek by default).
  2. Persist full structured call diagnostics (every attempt: messages, raw response,
     usage, timing) as session-local JSON sidecars when an agent session is bound,
     and otherwise to stage-local ``<stage>/llm/<agent>_<call_id>.diagnostics.json``
     sidecars. DynaDB-style markdown call logs are the default human-readable view;
     diagnostics JSON remains the machine-readable sidecar.
  3. Classify every failure into a typed LLMError with an :class:`Anomaly` kind.
  4. Retry transient transport faults forever by default (fixed legacy waits normally,
     full jitter in formal campaigns) and run a bounded JSON/schema *repair* loop.

Stub mode (``settings.stub``): no network; a registered ``stub_fn`` returns canned output
so the whole pipeline is exercisable offline and deterministically in tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import random
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, nullcontext
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse
from uuid import uuid4

try:  # ``flock`` is required only by formal file-backed campaign budgeting.
    import fcntl
except ImportError:  # pragma: no cover - OpenRouter campaign runner is Unix-only
    fcntl = None  # type: ignore[assignment]

import json5
from jsonschema import Draft202012Validator

from ..config import Settings
from ..errors import (
    Anomaly,
    CampaignPauseError,
    ContextOverflowError,
    EmptyResponseError,
    LLMError,
    LLMTimeoutError,
    PromptAnomalyError,
    RateLimitError,
    RefusalError,
    ResponseParseError,
    SchemaValidationError,
    TruncatedResponseError,
)
from ..observability import RunLogger
from .types import Message, ToolChoice, ToolLLMResult, ToolSchema, parse_tool_calls

if TYPE_CHECKING:
    from ..utils.logging import TaskLogger

StubFn = Callable[[str, list[Message], dict | None], "str | dict[str, Any]"]

_REFUSAL_MARKERS = (
    "i cannot help",
    "i can't help",
    "i cannot assist",
    "i'm unable to",
    "i am unable to",
    "i won't",
    "i will not",
    "as an ai",
)
_ALLOWED_ROLES = {"system", "user", "assistant", "tool", "developer"}
_ALLOWED_MESSAGE_KEYS = {
    "role",
    "content",
    "name",
    "tool_call_id",
    "tool_calls",
    "reasoning_content",
}
_DEEPSEEK_OPENAI_HOST = "api.deepseek.com"
_DEEPSEEK_TOOL_CHOICE_DISABLED_REASON = (
    "deepseek OpenAI-format thinking mode does not support tool_choice"
)
_BUDGET_LEDGER_SCHEMA = "tend.openrouter_campaign_budget.v3"
_COMPACTED_ATTEMPTS_KIND = "compacted_attempts"
_BUDGET_EVENT_LIMIT = 256
_LEDGER_EXECUTOR_WORKERS = 1


class BudgetReservationBusy(RuntimeError):
    """The cap is temporarily occupied by other in-flight provider requests."""

    def __init__(self, *, context: dict[str, Any]) -> None:
        super().__init__("OpenRouter campaign budget is temporarily fully reserved")
        self.context = context


class CampaignBudgetLedger:
    """Fail-closed cross-process state for one frozen OpenRouter campaign.

    The JSON file is atomically replaced while an independent, never-replaced ``.lock``
    inode is held.  Cap, worst-case request reservation, and pricing snapshot hash are
    immutable across processes.  Pauses survive restarts and require explicit resume.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        cap_usd: float,
        reservation_usd: float,
        pricing_profile_sha256: str,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.ack_root = self.path.with_name(f"{self.path.name}.attempt-acks")
        self.cap_usd = self._finite_nonnegative(cap_usd, "campaign budget cap")
        self.reservation_usd = self._finite_nonnegative(
            reservation_usd, "request reservation"
        )
        self.pricing_profile_sha256 = str(pricing_profile_sha256).strip().lower()
        if self.cap_usd <= 0:
            raise ValueError("campaign budget cap must be positive")
        if self.reservation_usd <= 0:
            raise ValueError("request reservation must be positive")
        if self.reservation_usd > self.cap_usd:
            raise ValueError("request reservation cannot exceed campaign budget cap")
        if not re.fullmatch(r"[0-9a-f]{64}", self.pricing_profile_sha256):
            raise ValueError("pricing_profile_sha256 must contain exactly 64 hex characters")

    def initialize(self) -> dict[str, Any]:
        """Create or validate the frozen ledger without changing spend counters."""

        return self._locked_update(lambda _state: None)

    def snapshot(self) -> dict[str, Any]:
        """Read and validate the durable state under the cross-process lock."""

        state = self._locked_update(None)
        accounted_remaining = (
            state["cap_usd"]
            - state["known_cost_usd"]
            - state["unknown_spend_usd"]
            - state["reserved_usd"]
        )
        state["accounted_remaining_usd"] = accounted_remaining
        state["effective_remaining_usd"] = (
            accounted_remaining - state["unaccounted_provider_cost_usd"]
        )
        # Backward-compatible name now uses the conservative, real-overage-aware value.
        state["remaining_usd"] = state["effective_remaining_usd"]
        return state

    def reserve(
        self,
        reservation_id: str,
        amount_usd: float,
        *,
        attempt_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reserve the frozen worst-case amount before a provider request is sent."""

        amount = self._finite_nonnegative(amount_usd, "request reservation")
        if amount <= 0:
            raise ValueError("request reservation must be positive")
        if not math.isclose(amount, self.reservation_usd, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("request reservation does not match the frozen ledger profile")
        if not isinstance(reservation_id, str) or not reservation_id.strip():
            raise ValueError("reservation_id must be a non-empty string")
        outcome: dict[str, Any] = {}

        def mutate(state: dict[str, Any]) -> None:
            if state["paused"]:
                outcome["paused"] = state["pause_reason"]
                return
            active = state["active_reservations"]
            if reservation_id in active:
                reason = "duplicate_budget_reservation"
                self._set_paused(state, reason, {"reservation_id": reservation_id})
                outcome["paused"] = reason
                return
            spent = state["known_cost_usd"] + state["unknown_spend_usd"]
            reserved = state["reserved_usd"]
            if spent + amount > self.cap_usd + 1e-12:
                reason = "campaign_budget_exhausted"
                self._set_paused(
                    state,
                    reason,
                    {
                        "known_plus_unknown_usd": spent,
                        "requested_reservation_usd": amount,
                    },
                )
                outcome["paused"] = reason
                return
            if spent + reserved + amount > self.cap_usd + 1e-12:
                outcome["busy"] = {
                    "ledger": str(self.path),
                    "cap_usd": self.cap_usd,
                    "known_plus_unknown_usd": spent,
                    "reserved_usd": reserved,
                    "requested_reservation_usd": amount,
                }
                return
            started_receipt = self._make_started_attempt_receipt(
                reservation_id=reservation_id,
                reservation_usd=amount,
                attempt_receipt=attempt_receipt,
            )
            started_id = str(started_receipt["attempt_receipt_id"])
            active_receipt_ids = {
                str(value.get("started_attempt_receipt", {}).get("attempt_receipt_id"))
                for value in active.values()
                if isinstance(value, dict)
            }
            if (
                started_id in active_receipt_ids
                or started_id in state["unacked_attempt_receipts"]
                or self._read_attempt_ack_marker(started_id) is not None
            ):
                reason = "duplicate_started_attempt_receipt"
                self._set_paused(
                    state,
                    reason,
                    {
                        "reservation_id": reservation_id,
                        "attempt_receipt_id": started_id,
                    },
                )
                outcome["paused"] = reason
                return
            active[reservation_id] = {
                "amount_usd": amount,
                "owner_pid": os.getpid(),
                "reserved_at": self._now(),
                "started_attempt_receipt": started_receipt,
            }
            state["reserved_usd"] = self._active_sum(active)
            state["started_attempt_count"] += 1
            outcome["attempt_receipt_id"] = started_id
            outcome["started_attempt_receipt_sha256"] = started_receipt[
                "started_attempt_receipt_sha256"
            ]

        state = self._locked_update(mutate)
        if "paused" in outcome:
            raise self._pause_error(str(outcome["paused"]))
        if "busy" in outcome:
            raise BudgetReservationBusy(context=outcome["busy"])
        state["attempt_receipt_id"] = outcome.get("attempt_receipt_id")
        state["started_attempt_receipt_sha256"] = outcome.get(
            "started_attempt_receipt_sha256"
        )
        return state

    def settle(
        self,
        reservation_id: str,
        *,
        known_cost_usd: float | None,
        call_id: str | None = None,
        provider_attempt_index: int | None = None,
        settlement_kind: str = "provider_attempt",
        pause_reason: str | None = None,
        raise_on_pause: bool = True,
        attempt_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Settle and optionally pause in one locked transaction.

        Unknown or invalid cost consumes the complete reservation.  Invalid cost pauses.
        A cost above the frozen reservation records the underestimation, accounts the
        reserved amount, stores the excess separately, and pauses.  This preserves both
        the hard-cap invariant and the evidence needed for operator reconciliation.
        """

        outcome: dict[str, Any] = {}

        def mutate(state: dict[str, Any]) -> None:
            preexisting_pause_reason = (
                str(state["pause_reason"]) if state["paused"] else None
            )
            active = state["active_reservations"]
            raw_reservation = active.pop(reservation_id, None)
            if raw_reservation is None:
                reason = "missing_budget_reservation"
                self._set_paused(state, reason, {"reservation_id": reservation_id})
                outcome["pause_reason"] = reason
                return
            amount, owner_pid = self._reservation_amount_and_owner(
                reservation_id, raw_reservation
            )
            started_attempt_receipt = (
                raw_reservation.get("started_attempt_receipt")
                if isinstance(raw_reservation, dict)
                else None
            )
            state["reserved_usd"] = self._active_sum(active)
            valid_cost = False
            cost = 0.0
            raw_cost_repr: str | None = None
            if known_cost_usd is not None:
                try:
                    cost = float(known_cost_usd)
                    valid_cost = math.isfinite(cost) and cost >= 0
                except (TypeError, ValueError):
                    raw_cost_repr = repr(known_cost_usd)
            effective_pause = pause_reason
            if known_cost_usd is None:
                state["unknown_spend_usd"] += amount
                accounting_kind = "unknown"
            elif not valid_cost:
                raw_cost_repr = raw_cost_repr or repr(known_cost_usd)
                state["unknown_spend_usd"] += amount
                accounting_kind = "invalid_provider_cost"
                effective_pause = "invalid_provider_cost"
            elif cost > amount + 1e-12:
                state["known_cost_usd"] += amount
                state["unaccounted_provider_cost_usd"] += cost - amount
                state["underestimated_reservation_count"] += 1
                accounting_kind = "reservation_underestimated"
                effective_pause = "provider_cost_exceeded_reservation"
            else:
                state["known_cost_usd"] += cost
                accounting_kind = "known"
            state["settled_attempts"] += 1
            event = {
                "event": "provider_reservation_settled",
                "reservation_id": reservation_id,
                "reservation_usd": amount,
                "owner_pid": owner_pid,
                "call_id": call_id,
                "provider_attempt_index": provider_attempt_index,
                "settlement_kind": settlement_kind,
                "accounting_kind": accounting_kind,
                "provider_cost_usd": cost if valid_cost else None,
                "provider_cost_raw": raw_cost_repr,
                "timestamp": self._now(),
            }
            if effective_pause:
                self._set_paused(state, effective_pause, event)
                outcome["pause_reason"] = effective_pause
                outcome["pause_triggered_by_settlement"] = True
            else:
                outcome["pause_triggered_by_settlement"] = False
                outcome["preexisting_pause_reason"] = preexisting_pause_reason

            receipt = self._make_attempt_recovery_receipt(
                state=state,
                reservation_id=reservation_id,
                reservation_usd=amount,
                call_id=call_id,
                provider_attempt_index=provider_attempt_index,
                settlement_kind=settlement_kind,
                accounting_kind=accounting_kind,
                valid_provider_cost_usd=(cost if valid_cost else None),
                provider_cost_raw=raw_cost_repr,
                attempt_receipt=attempt_receipt,
                started_attempt_receipt=started_attempt_receipt,
                pause_triggered_by_settlement=bool(
                    outcome["pause_triggered_by_settlement"]
                ),
                settlement_pause_reason=(
                    str(outcome.get("pause_reason"))
                    if outcome.get("pause_triggered_by_settlement")
                    else None
                ),
                preexisting_pause_reason=preexisting_pause_reason,
            )
            receipt_id = str(receipt["attempt_receipt_id"])
            receipt_sha256 = str(receipt["attempt_receipt_sha256"])
            pending = state["unacked_attempt_receipts"]
            prior = pending.get(receipt_id)
            prior_hash = (
                prior.get("attempt_receipt_sha256")
                if isinstance(prior, dict)
                else self._read_attempt_ack_marker(receipt_id)
            )
            if prior_hash is not None:
                reason = "duplicate_or_conflicting_attempt_receipt"
                self._set_paused(
                    state,
                    reason,
                    {
                        "attempt_receipt_id": receipt_id,
                        "existing_sha256": prior_hash,
                        "new_sha256": receipt_sha256,
                    },
                )
                outcome["pause_reason"] = reason
                outcome["pause_triggered_by_settlement"] = True
            else:
                pending[receipt_id] = receipt
                state["attempt_receipt_count"] += 1
            event["attempt_receipt_id"] = receipt_id
            event["attempt_receipt_sha256"] = receipt_sha256
            self._append_bounded(state, "settlement_events", event)
            state["settlement_event_count"] += 1
            outcome["attempt_receipt_id"] = receipt_id
            outcome["attempt_receipt_sha256"] = receipt_sha256

        state = self._locked_update(mutate)
        state["settlement_pause_triggered"] = bool(
            outcome.get("pause_triggered_by_settlement", False)
        )
        state["settlement_preexisting_pause_reason"] = outcome.get(
            "preexisting_pause_reason"
        )
        state["settlement_pause_reason"] = (
            outcome.get("pause_reason")
            if outcome.get("pause_triggered_by_settlement")
            else None
        )
        state["attempt_receipt_id"] = outcome.get("attempt_receipt_id")
        state["attempt_receipt_sha256"] = outcome.get("attempt_receipt_sha256")
        if raise_on_pause and outcome.get("pause_triggered_by_settlement"):
            raise self._pause_error(str(outcome["pause_reason"]))
        return state

    def pending_attempt_rows(self) -> list[dict[str, Any]]:
        """Return unacknowledged provider-attempt WAL rows in stable order."""

        state = self._locked_update(None)
        pending = state["unacked_attempt_receipts"]
        return [pending[key] for key in sorted(pending)]

    def verify_attempt_receipt_acks(self) -> dict[str, Any]:
        """Validate the sharded acknowledgement index against ledger counters."""

        summary: dict[str, Any] = {}

        def inspect(state: dict[str, Any]) -> None:
            markers: dict[str, str] = {}
            if self.ack_root.exists():
                for marker in sorted(self.ack_root.glob("*/*.json")):
                    receipt_id = marker.stem
                    if receipt_id in markers:
                        raise self._invalid_state(
                            "attempt acknowledgement index contains duplicate ids"
                        )
                    receipt_hash = self._read_attempt_ack_marker(receipt_id)
                    if receipt_hash is None:
                        raise self._invalid_state(
                            "attempt acknowledgement marker disappeared during audit"
                        )
                    markers[receipt_id] = receipt_hash
            expected = int(state["attempt_receipt_ack_count"])
            if len(markers) != expected:
                raise self._invalid_state(
                    "attempt acknowledgement marker count does not match ledger counter"
                )
            pending = state["unacked_attempt_receipts"]
            if set(markers) & set(pending):
                raise self._invalid_state(
                    "attempt receipt is both pending and acknowledged"
                )
            digest_input = "\n".join(
                f"{receipt_id}:{markers[receipt_id]}" for receipt_id in sorted(markers)
            ).encode("utf-8")
            summary.update(
                {
                    "ack_root": str(self.ack_root),
                    "acknowledged_count": len(markers),
                    "pending_count": len(pending),
                    "attempt_receipt_count": int(state["attempt_receipt_count"]),
                    "started_attempt_count": int(state["started_attempt_count"]),
                    "settled_attempts": int(state["settled_attempts"]),
                    "active_reservation_count": len(state["active_reservations"]),
                    "ack_index_sha256": hashlib.sha256(digest_input).hexdigest(),
                }
            )

        self._locked_update(inspect)
        return summary

    def acknowledge_attempt_receipt(
        self,
        attempt_receipt_id: str,
        attempt_receipt_sha256: str,
    ) -> dict[str, Any]:
        """Acknowledge one fsynced attempt row without permitting wildcard cleanup."""

        receipt_id = str(attempt_receipt_id).strip()
        receipt_hash = str(attempt_receipt_sha256).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", receipt_id):
            raise ValueError("attempt_receipt_id must contain 64 lowercase hex characters")
        if not re.fullmatch(r"[0-9a-f]{64}", receipt_hash):
            raise ValueError("attempt_receipt_sha256 must contain 64 hex characters")
        outcome: dict[str, Any] = {}

        def mutate(state: dict[str, Any]) -> None:
            pending = state["unacked_attempt_receipts"]
            receipt = pending.get(receipt_id)
            if receipt is None:
                prior_hash = self._read_attempt_ack_marker(receipt_id)
                if prior_hash == receipt_hash:
                    outcome["already_acknowledged"] = True
                    return
                reason = "attempt_receipt_ack_mismatch"
                self._set_paused(
                    state,
                    reason,
                    {
                        "attempt_receipt_id": receipt_id,
                        "expected_sha256": prior_hash,
                        "received_sha256": receipt_hash,
                    },
                )
                outcome["pause_reason"] = reason
                return
            expected_hash = receipt.get("attempt_receipt_sha256")
            if expected_hash != receipt_hash:
                reason = "attempt_receipt_ack_mismatch"
                self._set_paused(
                    state,
                    reason,
                    {
                        "attempt_receipt_id": receipt_id,
                        "expected_sha256": expected_hash,
                        "received_sha256": receipt_hash,
                    },
                )
                outcome["pause_reason"] = reason
                return
            self._write_attempt_ack_marker(receipt_id, receipt_hash)
            pending.pop(receipt_id)
            state["attempt_receipt_ack_count"] += 1
            outcome["acknowledged"] = True

        state = self._locked_update(mutate)
        if "pause_reason" in outcome:
            raise self._pause_error(str(outcome["pause_reason"]))
        return state

    def reconcile_attempt_receipts(
        self,
        sink_path: str | Path,
        *,
        receipt_enricher: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        receipt_filter: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Idempotently backfill pending WAL rows into one durable JSONL sink.

        The sink has its own permanent lock inode. A crash after append/fsync but
        before budget-ledger acknowledgement is safe: the next run finds the same
        receipt id and hash, then only acknowledges it. A conflicting row pauses the
        campaign instead of silently accepting different route or cost evidence.
        """

        path = Path(sink_path).expanduser().resolve()
        if path in {self.path, self.lock_path}:
            raise ValueError("attempt receipt sink must differ from the budget ledger")
        path.parent.mkdir(parents=True, exist_ok=True)
        sink_lock_path = path.with_name(f"{path.name}.lock")
        recovered = 0
        already_present = 0
        acknowledged = 0
        filtered_out = 0
        for receipt in self.pending_attempt_rows():
            if receipt_filter is not None and not bool(receipt_filter(dict(receipt))):
                filtered_out += 1
                continue
            receipt_id = str(receipt["attempt_receipt_id"])
            receipt_hash = str(receipt["attempt_receipt_sha256"])
            lock_fd = os.open(sink_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                existing = self._attempt_sink_index(path)
                prior = existing.get(receipt_id)
                if prior is not None and prior != receipt_hash:
                    state = self.pause(
                        "attempt_receipt_sink_mismatch",
                        details={
                            "sink": str(path),
                            "attempt_receipt_id": receipt_id,
                            "ledger_sha256": receipt_hash,
                            "sink_sha256": prior,
                        },
                    )
                    raise self._pause_error(
                        str(state.get("pause_reason") or "attempt_receipt_sink_mismatch")
                    )
                if prior is None:
                    row = json.loads(json.dumps(receipt))
                    if receipt_enricher is not None:
                        enriched = receipt_enricher(dict(row))
                        if not isinstance(enriched, dict):
                            raise TypeError("receipt_enricher must return an object")
                        row = enriched
                    if (
                        row.get("attempt_receipt_id") != receipt_id
                        or row.get("attempt_receipt_sha256") != receipt_hash
                    ):
                        raise ValueError(
                            "receipt_enricher cannot change receipt id or hash"
                        )
                    row["recovered_from_budget_wal"] = True
                    payload = (
                        json.dumps(
                            self._receipt_json_safe(row),
                            ensure_ascii=False,
                            sort_keys=True,
                            allow_nan=False,
                        )
                        + "\n"
                    ).encode("utf-8")
                    descriptor = os.open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                        0o600,
                    )
                    try:
                        offset = 0
                        while offset < len(payload):
                            offset += os.write(descriptor, payload[offset:])
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    recovered += 1
                else:
                    already_present += 1
                # Merely reading a complete row does not prove it survived the prior
                # writer's crash. Re-establish file and directory durability before
                # deleting the only WAL copy, including the already-present branch.
                self._fsync_attempt_sink(path)
                self.acknowledge_attempt_receipt(receipt_id, receipt_hash)
                acknowledged += 1
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        remaining = self.pending_attempt_rows()
        remaining_matching = (
            len(remaining)
            if receipt_filter is None
            else sum(1 for row in remaining if bool(receipt_filter(dict(row))))
        )
        return {
            "sink": str(path),
            "recovered": recovered,
            "already_present": already_present,
            "acknowledged": acknowledged,
            "filtered_out": filtered_out,
            "remaining_pending": len(remaining),
            "remaining_matching": remaining_matching,
        }

    def _make_started_attempt_receipt(
        self,
        *,
        reservation_id: str,
        reservation_usd: float,
        attempt_receipt: dict[str, Any] | None,
    ) -> dict[str, Any]:
        template = self._receipt_json_safe(attempt_receipt or {})
        if not isinstance(template, dict):
            raise ValueError("attempt_receipt must be an object")
        identity = self._attempt_receipt_identity(
            reservation_id=reservation_id,
            template=template,
            call_id=template.get("call_id"),
            provider_attempt_index=template.get("provider_attempt_index"),
        )
        receipt_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        started = {
            "attempt_receipt_id": receipt_id,
            "attempt_receipt_identity": identity,
            "reservation_id": reservation_id,
            "reservation_usd": reservation_usd,
            "owner_pid": os.getpid(),
            "started_at": self._now(),
            "status": "provider_request_started",
            "attempt_receipt_template": template,
        }
        canonical = json.dumps(
            started,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        started["started_attempt_receipt_sha256"] = hashlib.sha256(canonical).hexdigest()
        return started

    def _attempt_receipt_identity(
        self,
        *,
        reservation_id: str,
        template: dict[str, Any],
        call_id: Any,
        provider_attempt_index: Any,
    ) -> dict[str, Any]:
        context = template.get("receipt_context")
        if not isinstance(context, dict):
            context = {}
        task_id = str(template.get("task_id") or context.get("task_id") or "unknown")
        db_id = str(
            context.get("formal_db_id")
            or (task_id.split("/", 1)[0] if task_id != "unknown" else "unknown")
        )
        try:
            attempt_index = int(provider_attempt_index or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("provider_attempt_index must be an integer") from exc
        return {
            "campaign_binding": str(
                context.get("formal_campaign_manifest_sha256")
                or context.get("campaign_binding")
                or self.pricing_profile_sha256
            ),
            "arm_id": str(context.get("formal_arm_id") or template.get("agent") or "unknown"),
            "db_id": db_id,
            "task_id": task_id,
            "call_id": str(call_id or template.get("call_id") or "unknown"),
            "provider_attempt_index": attempt_index,
            "reservation_id": reservation_id,
        }

    def _make_attempt_recovery_receipt(
        self,
        *,
        state: dict[str, Any],
        reservation_id: str,
        reservation_usd: float,
        call_id: str | None,
        provider_attempt_index: int | None,
        settlement_kind: str,
        accounting_kind: str,
        valid_provider_cost_usd: float | None,
        provider_cost_raw: str | None,
        attempt_receipt: dict[str, Any] | None,
        started_attempt_receipt: dict[str, Any] | None,
        pause_triggered_by_settlement: bool,
        settlement_pause_reason: str | None,
        preexisting_pause_reason: str | None,
    ) -> dict[str, Any]:
        started = self._receipt_json_safe(started_attempt_receipt or {})
        if not isinstance(started, dict):
            raise ValueError("started_attempt_receipt must be an object")
        started_template = started.get("attempt_receipt_template")
        if not isinstance(started_template, dict):
            started_template = {}
        template = {
            **started_template,
            **self._receipt_json_safe(attempt_receipt or {}),
        }
        if not isinstance(template, dict):
            raise ValueError("attempt_receipt must be an object")
        context = template.get("receipt_context")
        if not isinstance(context, dict):
            context = {}
        task_id = str(template.get("task_id") or context.get("task_id") or "unknown")
        agent = str(template.get("agent") or "unknown")
        started_identity = started.get("attempt_receipt_identity")
        if isinstance(started_identity, dict):
            identity = started_identity
        else:
            identity = self._attempt_receipt_identity(
                reservation_id=reservation_id,
                template=template,
                call_id=call_id,
                provider_attempt_index=provider_attempt_index,
            )
        receipt_id = str(started.get("attempt_receipt_id") or "")
        expected_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if receipt_id and receipt_id != expected_id:
            raise ValueError("started attempt receipt id does not match its identity")
        receipt_id = expected_id
        raw_latency = template.get("latency_s", 0.0)
        latency = self._finite_nonnegative(raw_latency, "attempt receipt latency_s")
        response_received = bool(template.get("response_received", False))
        status = str(
            template.get("status")
            or (
                "response_received_pending_validation"
                if response_received
                else "provider_error_pending_retry_decision"
            )
        ).strip()
        if not status:
            raise ValueError("attempt receipt status must be non-empty")
        anomaly = template.get("anomaly")
        if not response_received and not str(anomaly or "").strip():
            anomaly = "api_error"
        usage = template.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        provider_metadata = template.get("provider_metadata")
        if not isinstance(provider_metadata, dict):
            provider_metadata = {}
        budget_reservation = template.get("budget_reservation")
        if not isinstance(budget_reservation, dict):
            budget_reservation = {}
        budget_reservation.update(
            {
                "reservation_id": reservation_id,
                "reservation_usd": reservation_usd,
            }
        )
        settled_cost = valid_provider_cost_usd
        budget_settlement = {
            "reservation_id": reservation_id,
            "settled_cost_usd": settled_cost,
            "settlement_kind": "known" if settled_cost is not None else "unknown",
            "accounting_kind": accounting_kind,
            "cap_usd": state["cap_usd"],
            "known_cost_usd": state["known_cost_usd"],
            "unknown_spend_usd": state["unknown_spend_usd"],
            "reserved_usd": state["reserved_usd"],
            "unaccounted_provider_cost_usd": state[
                "unaccounted_provider_cost_usd"
            ],
            "paused": state["paused"],
            "pause_reason": state["pause_reason"],
            "settlement_pause_reason": settlement_pause_reason,
            "settlement_pause_triggered": pause_triggered_by_settlement,
            "preexisting_pause_reason": preexisting_pause_reason,
        }
        row = {
            "record_type": "provider_attempt",
            "attempt_receipt_id": receipt_id,
            "attempt_receipt_identity": identity,
            "receipt_state": "settled_pending_durable_attempt_row",
            "call_id": identity["call_id"],
            "agent": agent,
            "model": template.get("model"),
            "provider_attempt_index": identity["provider_attempt_index"],
            "transport_attempt": int(template.get("transport_attempt", 0) or 0),
            "repair_index": int(template.get("repair_index", 0) or 0),
            "request_kind": (
                "initial" if int(template.get("repair_index", 0) or 0) == 0 else "json_repair"
            ),
            "timestamp": str(template.get("timestamp") or self._now()),
            "stage": template.get("stage"),
            "task_id": task_id,
            "status": status,
            "call_status": status,
            "response_received": response_received,
            "latency_s": latency,
            "finish_reason": template.get("finish_reason"),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get(
                "total_tokens",
                int(usage.get("prompt_tokens", 0) or 0)
                + int(usage.get("completion_tokens", 0) or 0),
            ),
            "cache_hit_tokens": usage.get("cache_hit_tokens", 0),
            "cache_miss_tokens": usage.get("cache_miss_tokens", 0),
            "provider_metadata": provider_metadata or None,
            "provider": provider_metadata.get("provider"),
            "openrouter_metadata": provider_metadata.get("openrouter_metadata"),
            "provider_cost_observed": template.get(
                "provider_cost_observed", provider_cost_raw
            ),
            "cost_usd": valid_provider_cost_usd,
            "settled_cost_usd": settled_cost,
            "cost_source": template.get("cost_source") or accounting_kind,
            "anomaly": anomaly,
            "error": template.get("error"),
            "request_config": template.get("request_config") or {},
            "budget_reservation": budget_reservation,
            "budget_settlement": budget_settlement,
            "settlement_kind_detail": settlement_kind,
            "recovered_from_budget_wal": True,
        }
        row = self._receipt_json_safe(row)
        canonical = json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        row["attempt_receipt_sha256"] = hashlib.sha256(canonical).hexdigest()
        return row

    @staticmethod
    def _receipt_json_safe(value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            return {"nonfinite_float": repr(value)}
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {
                str(key): CampaignBudgetLedger._receipt_json_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [CampaignBudgetLedger._receipt_json_safe(item) for item in value]
        return _safe_repr(value)

    def _attempt_sink_index(self, path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        data = path.read_bytes()
        if data and not data.endswith(b"\n"):
            last_newline = data.rfind(b"\n")
            descriptor = os.open(path, os.O_RDWR)
            try:
                os.ftruncate(descriptor, last_newline + 1)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            data = data[: last_newline + 1]
        index: dict[str, str] = {}
        for line_number, raw_line in enumerate(data.splitlines(), start=1):
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise CampaignPauseError(
                    "attempt receipt sink contains invalid JSON",
                    context={
                        "pause_reason": "attempt_receipt_sink_invalid",
                        "sink": str(path),
                        "line": line_number,
                    },
                ) from exc
            receipt_id = row.get("attempt_receipt_id")
            receipt_hash = row.get("attempt_receipt_sha256")
            if receipt_id is None:
                continue
            if not isinstance(receipt_id, str) or not re.fullmatch(
                r"[0-9a-f]{64}", str(receipt_hash or "")
            ):
                raise CampaignPauseError(
                    "attempt receipt sink contains an invalid receipt identity",
                    context={
                        "pause_reason": "attempt_receipt_sink_invalid",
                        "sink": str(path),
                        "line": line_number,
                    },
                )
            if receipt_id in index:
                raise CampaignPauseError(
                    "attempt receipt sink contains a duplicate receipt id",
                    context={
                        "pause_reason": "attempt_receipt_sink_duplicate",
                        "sink": str(path),
                        "attempt_receipt_id": receipt_id,
                    },
                )
            index[receipt_id] = str(receipt_hash)
        return index

    def pause(self, reason: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
        """Persist a campaign-wide pause without changing reservations."""

        normalized = str(reason).strip()
        if not normalized:
            raise ValueError("pause reason must be non-empty")

        def mutate(state: dict[str, Any]) -> None:
            self._set_paused(state, normalized, details or {})

        return self._locked_update(mutate)

    def resume_campaign(self, operator_reason: str) -> dict[str, Any]:
        """Explicitly resume a safe ledger; the client never calls this automatically.

        A reservation underestimation leaves unaccounted provider cost and cannot be
        resumed in place. It requires operator reconciliation and an explicitly
        authorized successor pricing profile/ledger so the original evidence remains.
        """

        reason = str(operator_reason).strip()
        if not reason:
            raise ValueError("operator_reason must be non-empty")
        outcome: dict[str, str] = {}

        def mutate(state: dict[str, Any]) -> None:
            if state["active_reservations"]:
                outcome["blocked"] = "active_reservations_remain"
                return
            if state["unaccounted_provider_cost_usd"] > 0:
                outcome["blocked"] = "unaccounted_provider_cost_remains"
                return
            if (
                state["known_cost_usd"]
                + state["unknown_spend_usd"]
                + self.reservation_usd
                > self.cap_usd + 1e-12
            ):
                outcome["blocked"] = "campaign_budget_exhausted"
                return
            if state["paused"]:
                self._append_bounded(
                    state,
                    "pause_events",
                    {
                        "event": "campaign_resumed",
                        "operator_reason": reason,
                        "prior_pause_reason": state["pause_reason"],
                        "timestamp": self._now(),
                    },
                )
                state["paused"] = False
                state["pause_reason"] = None
                state["paused_at"] = None
                state["resume_count"] += 1

        state = self._locked_update(mutate)
        if "blocked" in outcome:
            raise self._pause_error(outcome["blocked"])
        return state

    def reconcile_process(self, owner_pid: int) -> dict[str, Any]:
        """Move reservations of one caller-confirmed dead PID to unknown spend."""

        if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
            raise ValueError("owner_pid must be a positive integer")

        def mutate(state: dict[str, Any]) -> None:
            active = state["active_reservations"]
            matched: list[tuple[str, float, dict[str, Any]]] = []
            for reservation_id, raw_reservation in list(active.items()):
                amount, reservation_owner = self._reservation_amount_and_owner(
                    reservation_id, raw_reservation
                )
                if reservation_owner == owner_pid:
                    matched.append((reservation_id, amount, raw_reservation))
            recovered_amount = math.fsum(amount for _key, amount, _raw in matched)
            for reservation_id, _amount, _raw in matched:
                active.pop(reservation_id)
            state["reserved_usd"] = self._active_sum(active)
            state["unknown_spend_usd"] += recovered_amount
            now = self._now()
            preexisting_pause_reason = (
                str(state["pause_reason"]) if state["paused"] else None
            )
            finalized_receipt_ids: list[str] = []
            for reservation_id, amount, raw_reservation in matched:
                started = raw_reservation["started_attempt_receipt"]
                identity = started["attempt_receipt_identity"]
                try:
                    started_at = datetime.fromisoformat(str(started["started_at"]))
                    latency_s = max(
                        0.0,
                        (datetime.now(timezone.utc) - started_at).total_seconds(),
                    )
                except (KeyError, TypeError, ValueError):
                    latency_s = 0.0
                state["settled_attempts"] += 1
                receipt = self._make_attempt_recovery_receipt(
                    state=state,
                    reservation_id=reservation_id,
                    reservation_usd=amount,
                    call_id=str(identity.get("call_id") or "unknown"),
                    provider_attempt_index=int(
                        identity.get("provider_attempt_index") or 0
                    ),
                    settlement_kind="dead_process_reconciliation",
                    accounting_kind="unknown_process_exit",
                    valid_provider_cost_usd=None,
                    provider_cost_raw=None,
                    attempt_receipt={
                        "status": "error",
                        "response_received": False,
                        "latency_s": latency_s,
                        "anomaly": "process_exit",
                        "error": {
                            "error_type": "ProviderWorkerProcessExit",
                            "message": "provider worker exited before attempt settlement",
                            "anomaly": "process_exit",
                            "retryable": True,
                            "context": {
                                "owner_pid": owner_pid,
                                "confirmed_dead_by_caller": True,
                                "continuation_required": True,
                            },
                        },
                        "cost_source": "unknown_process_exit",
                    },
                    started_attempt_receipt=started,
                    pause_triggered_by_settlement=False,
                    settlement_pause_reason=None,
                    preexisting_pause_reason=preexisting_pause_reason,
                )
                receipt["continuation_required"] = True
                receipt["prior_call_terminated_by_process_exit"] = True
                # The two continuation fields are part of the immutable receipt.
                receipt.pop("attempt_receipt_sha256", None)
                canonical = json.dumps(
                    receipt,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                receipt["attempt_receipt_sha256"] = hashlib.sha256(canonical).hexdigest()
                receipt_id = str(receipt["attempt_receipt_id"])
                if (
                    receipt_id in state["unacked_attempt_receipts"]
                    or self._read_attempt_ack_marker(receipt_id) is not None
                ):
                    raise self._invalid_state(
                        "dead-process attempt receipt collides with settled evidence"
                    )
                state["unacked_attempt_receipts"][receipt_id] = receipt
                state["attempt_receipt_count"] += 1
                state["settlement_event_count"] += 1
                self._append_bounded(
                    state,
                    "settlement_events",
                    {
                        "event": "provider_reservation_settled",
                        "reservation_id": reservation_id,
                        "reservation_usd": amount,
                        "owner_pid": owner_pid,
                        "call_id": identity.get("call_id"),
                        "provider_attempt_index": identity.get(
                            "provider_attempt_index"
                        ),
                        "settlement_kind": "dead_process_reconciliation",
                        "accounting_kind": "unknown_process_exit",
                        "provider_cost_usd": None,
                        "provider_cost_raw": None,
                        "attempt_receipt_id": receipt_id,
                        "attempt_receipt_sha256": receipt[
                            "attempt_receipt_sha256"
                        ],
                        "timestamp": now,
                    },
                )
                finalized_receipt_ids.append(receipt_id)
            state["reconciliation_count"] += 1
            state["reconciled_reservations_total"] += len(matched)
            self._append_bounded(
                state,
                "reconciliation_events",
                {
                    "event": "dead_process_reservations_to_unknown",
                    "owner_pid": owner_pid,
                    "matched_reservation_count": len(matched),
                    "amount_usd": recovered_amount,
                    "reservation_ids": [key for key, _amount, _raw in matched],
                    "attempt_receipt_ids": finalized_receipt_ids,
                    "confirmed_dead_by_caller": True,
                    "timestamp": self._now(),
                },
            )

        return self._locked_update(mutate)

    @staticmethod
    def _reservation_amount_and_owner(
        reservation_id: str,
        raw_reservation: Any,
    ) -> tuple[float, int | None]:
        if not isinstance(raw_reservation, dict):
            raise CampaignPauseError(
                "OpenRouter campaign ledger has an unstructured reservation",
                context={"reservation_id": reservation_id},
            )
        raw_amount = raw_reservation.get("amount_usd")
        raw_owner = raw_reservation.get("owner_pid")
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError) as exc:
            raise CampaignPauseError(
                "OpenRouter campaign ledger has an invalid reservation amount",
                context={"reservation_id": reservation_id, "raw_amount": raw_amount},
            ) from exc
        if not math.isfinite(amount) or amount <= 0:
            raise CampaignPauseError(
                "OpenRouter campaign ledger has an invalid reservation amount",
                context={"reservation_id": reservation_id, "raw_amount": raw_amount},
            )
        if isinstance(raw_owner, bool):
            owner = None
        else:
            try:
                owner = int(raw_owner) if raw_owner is not None else None
            except (TypeError, ValueError):
                owner = None
        return amount, owner if owner is not None and owner > 0 else None

    def _validate_started_attempt_receipt(
        self,
        reservation_id: str,
        receipt: Any,
    ) -> None:
        if not isinstance(receipt, dict):
            raise self._invalid_state(
                f"reservation {reservation_id!r} lacks a started attempt receipt"
            )
        receipt_id = str(receipt.get("attempt_receipt_id") or "")
        receipt_hash = str(receipt.get("started_attempt_receipt_sha256") or "")
        identity = receipt.get("attempt_receipt_identity")
        if not isinstance(identity, dict) or identity.get("reservation_id") != reservation_id:
            raise self._invalid_state("started attempt receipt identity is invalid")
        expected_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if receipt_id != expected_id:
            raise self._invalid_state("started attempt receipt id is invalid")
        core = dict(receipt)
        core.pop("started_attempt_receipt_sha256", None)
        expected_hash = hashlib.sha256(
            json.dumps(
                core,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if receipt_hash != expected_hash:
            raise self._invalid_state("started attempt receipt hash is invalid")

    def _validate_final_attempt_receipt(self, receipt_id: str, receipt: Any) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", str(receipt_id)):
            raise self._invalid_state("unacknowledged attempt receipt id is invalid")
        if not isinstance(receipt, dict):
            raise self._invalid_state("unacknowledged attempt receipt must be an object")
        if receipt.get("attempt_receipt_id") != receipt_id:
            raise self._invalid_state("unacknowledged attempt receipt id is inconsistent")
        receipt_hash = str(receipt.get("attempt_receipt_sha256") or "")
        core = dict(receipt)
        core.pop("attempt_receipt_sha256", None)
        expected_hash = hashlib.sha256(
            json.dumps(
                core,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if receipt_hash != expected_hash:
            raise self._invalid_state("unacknowledged attempt receipt hash is invalid")

    def _active_sum(self, active: dict[str, Any]) -> float:
        return math.fsum(
            self._reservation_amount_and_owner(key, value)[0]
            for key, value in active.items()
        )

    def _locked_update(
        self,
        mutate: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        if fcntl is None:
            raise CampaignPauseError(
                "cross-process budget locking is unavailable on this platform",
                context={"pause_reason": "budget_lock_unavailable", "ledger": str(self.path)},
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            state = self._read_state()
            if mutate is not None:
                mutate(state)
                self._validate_state(state)
                state["updated_at"] = self._now()
                self._write_state(state)
            return json.loads(json.dumps(state))
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def _read_state(self) -> dict[str, Any]:
        if not self.path.exists():
            state = {
                "schema": _BUDGET_LEDGER_SCHEMA,
                "cap_usd": self.cap_usd,
                "reservation_usd": self.reservation_usd,
                "pricing_profile_sha256": self.pricing_profile_sha256,
                "known_cost_usd": 0.0,
                "unknown_spend_usd": 0.0,
                "reserved_usd": 0.0,
                "unaccounted_provider_cost_usd": 0.0,
                "started_attempt_count": 0,
                "settled_attempts": 0,
                "settlement_event_count": 0,
                "settlement_events": [],
                "attempt_receipt_count": 0,
                "attempt_receipt_ack_count": 0,
                "unacked_attempt_receipts": {},
                "underestimated_reservation_count": 0,
                "reconciliation_count": 0,
                "reconciled_reservations_total": 0,
                "reconciliation_events": [],
                "active_reservations": {},
                "paused": False,
                "pause_reason": None,
                "paused_at": None,
                "pause_count": 0,
                "resume_count": 0,
                "pause_events": [],
                "updated_at": self._now(),
            }
            self._validate_state(state)
            return state
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CampaignPauseError(
                "OpenRouter campaign budget ledger is unreadable",
                context={
                    "pause_reason": "budget_ledger_unreadable",
                    "ledger": str(self.path),
                    "error": str(exc),
                },
            ) from exc
        if not isinstance(state, dict) or state.get("schema") != _BUDGET_LEDGER_SCHEMA:
            raise CampaignPauseError(
                "OpenRouter campaign budget ledger schema mismatch",
                context={
                    "pause_reason": "budget_ledger_schema_mismatch",
                    "ledger": str(self.path),
                    "expected_schema": _BUDGET_LEDGER_SCHEMA,
                },
            )
        self._validate_state(state)
        return state

    def _validate_state(self, state: dict[str, Any]) -> None:
        try:
            for key in (
                "cap_usd",
                "reservation_usd",
                "known_cost_usd",
                "unknown_spend_usd",
                "reserved_usd",
                "unaccounted_provider_cost_usd",
            ):
                state[key] = self._finite_nonnegative(state[key], key)
            for key in (
                "started_attempt_count",
                "settled_attempts",
                "settlement_event_count",
                "attempt_receipt_count",
                "attempt_receipt_ack_count",
                "underestimated_reservation_count",
                "reconciliation_count",
                "reconciled_reservations_total",
                "pause_count",
                "resume_count",
            ):
                raw = state[key]
                if isinstance(raw, bool):
                    raise ValueError(f"{key} cannot be boolean")
                value = int(raw)
                if value < 0 or value != raw:
                    raise ValueError(f"{key} must be a non-negative integer")
                state[key] = value
        except (KeyError, TypeError, ValueError) as exc:
            raise CampaignPauseError(
                "OpenRouter campaign budget ledger contains invalid values",
                context={
                    "pause_reason": "budget_ledger_invalid",
                    "ledger": str(self.path),
                    "error": str(exc),
                },
            ) from exc
        if not math.isclose(state["cap_usd"], self.cap_usd, rel_tol=0.0, abs_tol=1e-12):
            raise self._frozen_mismatch("cap_usd", state["cap_usd"], self.cap_usd)
        if not math.isclose(
            state["reservation_usd"], self.reservation_usd, rel_tol=0.0, abs_tol=1e-12
        ):
            raise self._frozen_mismatch(
                "reservation_usd", state["reservation_usd"], self.reservation_usd
            )
        if state.get("pricing_profile_sha256") != self.pricing_profile_sha256:
            raise self._frozen_mismatch(
                "pricing_profile_sha256",
                state.get("pricing_profile_sha256"),
                self.pricing_profile_sha256,
            )
        active = state.get("active_reservations")
        if not isinstance(active, dict):
            raise self._invalid_state("active_reservations must be an object")
        for key, value in active.items():
            amount, owner = self._reservation_amount_and_owner(key, value)
            if owner is None:
                raise self._invalid_state(f"reservation {key!r} has no positive owner PID")
            if not math.isclose(
                amount, self.reservation_usd, rel_tol=0.0, abs_tol=1e-12
            ):
                raise self._invalid_state(
                    f"reservation {key!r} does not match frozen reservation_usd"
                )
            started_receipt = value.get("started_attempt_receipt")
            self._validate_started_attempt_receipt(key, started_receipt)
        active_sum = self._active_sum(active)
        if not math.isclose(
            state["reserved_usd"], active_sum, rel_tol=0.0, abs_tol=1e-12
        ):
            raise self._invalid_state(
                "reserved_usd does not equal the sum of active reservations"
            )
        total = (
            state["known_cost_usd"]
            + state["unknown_spend_usd"]
            + state["reserved_usd"]
        )
        if total > self.cap_usd + 1e-12:
            raise self._invalid_state("known + unknown + reserved exceeds campaign cap")
        if not isinstance(state.get("paused"), bool):
            raise self._invalid_state("paused must be boolean")
        if state["paused"]:
            if not isinstance(state.get("pause_reason"), str) or not state["pause_reason"]:
                raise self._invalid_state("paused ledger requires pause_reason")
            if not isinstance(state.get("paused_at"), str) or not state["paused_at"]:
                raise self._invalid_state("paused ledger requires paused_at")
        elif state.get("pause_reason") is not None or state.get("paused_at") is not None:
            raise self._invalid_state("running ledger cannot retain pause fields")
        if state["unaccounted_provider_cost_usd"] > 0 and not state["paused"]:
            raise self._invalid_state("unaccounted provider cost requires a pause")
        pending = state.get("unacked_attempt_receipts")
        if not isinstance(pending, dict):
            raise self._invalid_state("unacked_attempt_receipts must be an object")
        active_receipt_ids: set[str] = set()
        for reservation_id, reservation in active.items():
            started = reservation["started_attempt_receipt"]
            active_receipt_ids.add(str(started["attempt_receipt_id"]))
        if active_receipt_ids & set(pending):
            raise self._invalid_state("active attempt receipt collides with a settled receipt")
        for receipt_id, receipt in pending.items():
            self._validate_final_attempt_receipt(receipt_id, receipt)
        if state["attempt_receipt_count"] != len(pending) + state[
            "attempt_receipt_ack_count"
        ]:
            raise self._invalid_state(
                "attempt_receipt_count does not match pending plus acknowledged receipts"
            )
        if state["attempt_receipt_count"] != state["settled_attempts"]:
            raise self._invalid_state(
                "every settled attempt must have one recovery receipt"
            )
        if state["started_attempt_count"] != state["settled_attempts"] + len(active):
            raise self._invalid_state(
                "started attempts must equal settled attempts plus active reservations"
            )
        for key in ("settlement_events", "reconciliation_events", "pause_events"):
            if not isinstance(state.get(key), list):
                raise self._invalid_state(f"{key} must be an array")

    def _set_paused(
        self,
        state: dict[str, Any],
        reason: str,
        details: dict[str, Any],
    ) -> None:
        now = self._now()
        if not state["paused"]:
            state["paused"] = True
            state["pause_reason"] = reason
            state["paused_at"] = now
            state["pause_count"] += 1
        self._append_bounded(
            state,
            "pause_events",
            {
                "event": "campaign_paused",
                "reason": reason,
                "root_pause_reason": state["pause_reason"],
                "details": _json_safe(details),
                "timestamp": now,
            },
        )

    @staticmethod
    def _append_bounded(state: dict[str, Any], key: str, event: dict[str, Any]) -> None:
        events = state[key]
        events.append(event)
        if len(events) > _BUDGET_EVENT_LIMIT:
            del events[: len(events) - _BUDGET_EVENT_LIMIT]

    @staticmethod
    def _finite_nonnegative(value: Any, name: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be finite and non-negative") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError(f"{name} must be finite and non-negative")
        return parsed

    def _pause_error(self, reason: str) -> CampaignPauseError:
        return CampaignPauseError(
            "OpenRouter campaign is durably paused",
            context={"pause_reason": reason, "ledger": str(self.path)},
        )

    def _invalid_state(self, message: str) -> CampaignPauseError:
        return CampaignPauseError(
            f"OpenRouter campaign budget ledger is invalid: {message}",
            context={"pause_reason": "budget_ledger_invalid", "ledger": str(self.path)},
        )

    def _frozen_mismatch(self, key: str, ledger: Any, configured: Any) -> CampaignPauseError:
        return CampaignPauseError(
            f"OpenRouter campaign {key} does not match the frozen ledger",
            context={
                "pause_reason": "budget_profile_mismatch",
                "ledger": str(self.path),
                "field": key,
                "ledger_value": ledger,
                "configured_value": configured,
            },
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _write_state(self, state: dict[str, Any]) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    state,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if tmp.exists():
                tmp.unlink()

    def _attempt_ack_marker_path(self, receipt_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", str(receipt_id)):
            raise ValueError("attempt_receipt_id must contain 64 lowercase hex characters")
        root = self.ack_root.resolve()
        marker = (root / receipt_id[:2] / f"{receipt_id}.json").resolve()
        if root not in marker.parents:
            raise ValueError("attempt receipt acknowledgement path escapes ack_root")
        return marker

    @staticmethod
    def _fsync_attempt_sink(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def _read_attempt_ack_marker(self, receipt_id: str) -> str | None:
        marker = self._attempt_ack_marker_path(receipt_id)
        if not marker.exists():
            return None
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CampaignPauseError(
                "provider-attempt acknowledgement marker is unreadable",
                context={
                    "pause_reason": "attempt_receipt_ack_marker_invalid",
                    "marker": str(marker),
                },
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("attempt_receipt_id") != receipt_id
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(payload.get("attempt_receipt_sha256") or "")
            )
        ):
            raise CampaignPauseError(
                "provider-attempt acknowledgement marker is invalid",
                context={
                    "pause_reason": "attempt_receipt_ack_marker_invalid",
                    "marker": str(marker),
                },
            )
        return str(payload["attempt_receipt_sha256"])

    def _write_attempt_ack_marker(self, receipt_id: str, receipt_hash: str) -> None:
        prior_hash = self._read_attempt_ack_marker(receipt_id)
        if prior_hash is not None:
            if prior_hash != receipt_hash:
                raise CampaignPauseError(
                    "provider-attempt acknowledgement marker conflicts with WAL",
                    context={
                        "pause_reason": "attempt_receipt_ack_marker_mismatch",
                        "attempt_receipt_id": receipt_id,
                        "marker_sha256": prior_hash,
                        "wal_sha256": receipt_hash,
                    },
                )
            return
        marker = self._attempt_ack_marker_path(receipt_id)
        shard_existed = marker.parent.exists()
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_name(f".{marker.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "attempt_receipt_id": receipt_id,
                        "attempt_receipt_sha256": receipt_hash,
                        "acknowledged_at": self._now(),
                    },
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, marker)
            dir_fd = os.open(marker.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            if not shard_existed:
                root_fd = os.open(self.ack_root, os.O_RDONLY)
                try:
                    os.fsync(root_fd)
                finally:
                    os.close(root_fd)
        finally:
            if tmp.exists():
                tmp.unlink()


@dataclass
class LLMResult:
    """A completed, validated model call."""

    agent: str
    call_id: str
    model: str
    text: str
    parsed: Any | None  # parsed JSON when expect_json/schema given
    finish_reason: str | None
    usage: dict[str, int]
    latency_s: float
    attempts: int
    transcript_ref: str  # primary call artifact ref under run dir
    diagnostics_ref: str = ""  # structured sidecar ref under run dir

    def __post_init__(self) -> None:
        if not self.diagnostics_ref:
            self.diagnostics_ref = _diagnostics_ref_from_transcript(self.transcript_ref)

    @property
    def data(self) -> dict[str, Any]:
        if not isinstance(self.parsed, dict):
            raise SchemaValidationError(
                "expected a JSON object result", context={"got_type": type(self.parsed).__name__}
            )
        return self.parsed


def _diagnostics_ref_from_transcript(transcript_ref: str) -> str:
    if transcript_ref.endswith(".md"):
        return f"{transcript_ref[:-3]}.diagnostics.json"
    return transcript_ref


def _llm_diagnostics_ref(
    log: RunLogger,
    agent: str,
    call_id: str,
    *,
    transcript_ref: str | None = None,
) -> str:
    ref_for_call = getattr(log, "llm_diagnostics_ref", None)
    if callable(ref_for_call):
        return str(ref_for_call(agent, call_id, transcript_ref=transcript_ref))
    return f"{agent}/llm/{agent}_{call_id}.diagnostics.json"


def _strip_code_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
        # drop a leading ``json`` language tag if it survived
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    return s.strip()


def _extract_json(text: str) -> Any:
    """Best-effort parse: strict JSON, then code-fence strip, then json5 (lenient)."""
    candidates = [text, _strip_code_fence(text)]
    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
    # last resort: locate the outermost {...} or [...] and json5-parse it
    for cand in candidates:
        start = min((cand.find(c) for c in "{[" if cand.find(c) >= 0), default=-1)
        if start >= 0:
            end = max(cand.rfind("}"), cand.rfind("]"))
            if end > start:
                try:
                    return json5.loads(cand[start : end + 1])
                except Exception:  # noqa: BLE001 - json5 raises broad ValueError subclasses
                    pass
    raise ResponseParseError("response is not valid JSON", context={"preview": text[:300]})


def _schema_errors(data: Any, schema: dict) -> list[str]:
    validator = Draft202012Validator(schema)
    errs = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    out = []
    for e in errs[:8]:
        loc = "$" + "".join(f"[{p!r}]" for p in e.path)
        out.append(f"{loc}: {e.message}")
    return out


def _json_safe(value: Any, *, max_depth: int = 8) -> Any:
    if max_depth < 0:
        return _safe_repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): _json_safe(v, max_depth=max_depth - 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, max_depth=max_depth - 1) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value), max_depth=max_depth - 1)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump(mode="json"), max_depth=max_depth - 1)
        except TypeError:
            return _json_safe(model_dump(), max_depth=max_depth - 1)
        except Exception:  # noqa: BLE001 - fall through to safer object handling
            pass
    if hasattr(value, "__dict__"):
        return {
            str(k): _json_safe(v, max_depth=max_depth - 1)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
    return _safe_repr(value)


def _safe_repr(value: Any, limit: int = 1200) -> str:
    text = repr(value)
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _provider_metadata(raw: Any, finish: str | None) -> dict[str, Any]:
    safe = _json_safe(raw)
    metadata: dict[str, Any] = {"finish_reason": finish}
    if isinstance(safe, dict):
        for key, value in safe.items():
            if _is_metadata_scalar(value) or key in {
                "finish_reason",
                "refusal",
                "truncation",
                "incomplete_details",
                "status",
                "error",
            }:
                metadata.setdefault(key, value)
        choices = safe.get("choices")
        if isinstance(choices, list):
            metadata["choices"] = [_choice_metadata(choice) for choice in choices]
        # OpenRouter emits its opt-in routing receipt as a nested object.  For
        # streaming chat completions it appears only on the terminal chunk, which
        # ``_collect_completion_stream`` retains inside ``stream_chunk_samples``.
        # Preserve it explicitly: the generic scalar-only metadata filter above is
        # intentionally too conservative to retain arbitrary nested response data.
        router_metadata = _openrouter_metadata_from_response(safe)
        if router_metadata is not None:
            metadata["openrouter_metadata"] = router_metadata
        provider_usage = _provider_usage_from_response(safe)
        if provider_usage is not None:
            metadata["provider_usage"] = provider_usage
        for key, value in _stream_response_scalars(safe).items():
            metadata.setdefault(key, value)
    return metadata


def _openrouter_metadata_from_response(safe: dict[str, Any]) -> dict[str, Any] | None:
    direct = safe.get("openrouter_metadata")
    if isinstance(direct, dict):
        return direct
    samples = safe.get("stream_chunk_samples")
    if not isinstance(samples, list):
        return None
    for sample in reversed(samples):
        if isinstance(sample, dict) and isinstance(sample.get("openrouter_metadata"), dict):
            return sample["openrouter_metadata"]
    return None


def _provider_usage_from_response(safe: dict[str, Any]) -> dict[str, Any] | None:
    direct = safe.get("provider_usage") or safe.get("usage")
    if isinstance(direct, dict):
        return direct
    samples = safe.get("stream_chunk_samples")
    if not isinstance(samples, list):
        return None
    for sample in reversed(samples):
        if isinstance(sample, dict) and isinstance(sample.get("usage"), dict):
            return sample["usage"]
    return None


def _stream_response_scalars(safe: dict[str, Any]) -> dict[str, Any]:
    samples = safe.get("stream_chunk_samples")
    if not isinstance(samples, list):
        return {}
    out: dict[str, Any] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        for key in ("id", "model", "created", "provider"):
            value = sample.get(key)
            if _is_metadata_scalar(value) and value is not None:
                out[key] = value
    return out


def _choice_metadata(choice: Any) -> dict[str, Any]:
    if not isinstance(choice, dict):
        return {"raw": choice}
    out: dict[str, Any] = {}
    for key in (
        "index",
        "finish_reason",
        "stop_reason",
        "truncation",
        "incomplete_details",
        "content_filter_results",
    ):
        if key in choice:
            out[key] = choice[key]
    message = choice.get("message")
    if isinstance(message, dict):
        msg: dict[str, Any] = {}
        for key in ("role", "refusal", "reasoning_content", "annotations"):
            if key in message:
                msg[key] = message[key]
        if "content" in message:
            msg["content_preview"] = str(message.get("content") or "")[:500]
        if msg:
            out["message"] = msg
    return out


def _is_metadata_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _metadata_refusal(provider_metadata: dict[str, Any] | None) -> Any | None:
    if not provider_metadata:
        return None
    refusal = provider_metadata.get("refusal")
    if refusal:
        return refusal
    for choice in provider_metadata.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict) and message.get("refusal"):
            return message["refusal"]
    return None


def _retry_after_seconds(exc: Exception) -> tuple[float | None, str | None]:
    """Extract Retry-After seconds (delta or HTTP date) from a provider exception."""

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    raw: Any = None
    if headers is not None:
        getter = getattr(headers, "get", None)
        if callable(getter):
            raw = getter("retry-after") or getter("Retry-After")
    if raw is None:
        return None, None
    text = str(raw).strip()
    try:
        seconds = float(text)
        return (max(0.0, seconds), text) if math.isfinite(seconds) else (None, text)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds()), text
    except (TypeError, ValueError, OverflowError):
        return None, text


# One HTTP/2 connection admits at most 100 concurrent streams. Stay under that
# so the 101st call opens another client instead of raising LocalProtocolError.
HTTP2_MAX_STREAMS_PER_CLIENT = 80


@dataclass
class _Http2Shard:
    client: Any
    in_flight: int = 0
    retired: bool = False


class LLMClient:
    """Async transcripting LLM client shared across all agents.

    The single provider call is concurrency-limited by a semaphore gate
    (configurable via ``settings.llm.max_concurrency``; ``<= 0`` — the default —
    runs unbounded), making this the one canonical chokepoint for live LLM
    throughput. Transient provider/transport faults use full-jitter exponential
    retries, forever when ``max_retries < 0`` (the default), so provider capacity
    errors reroute rather than become benchmark failures.
    """

    def __init__(self, settings: Settings, logger: RunLogger) -> None:
        self._s = settings
        self._log = logger
        self._stub_fn: StubFn | None = None
        self._client: Any = None
        self._tool_choice_unsupported_models: set[str] = set()
        self.on_usage: Callable[..., None] | None = None
        self.on_retry: Callable[..., None] | None = None
        self.on_provider_wait: Callable[..., None] | None = None
        self.on_provider_ok: Callable[[], None] | None = None
        self._progress_callback_failures_seen: set[str] = set()
        self._budget: CampaignBudgetLedger | None = None
        self._ledger_executor: ThreadPoolExecutor | None = None
        self._ledger_initialized = False
        self._ledger_init_lock = asyncio.Lock()
        llm_cfg = settings.llm
        campaign_requested = (
            llm_cfg.openrouter_campaign_budget_usd != 0
            or bool(llm_cfg.openrouter_budget_ledger.strip())
            or llm_cfg.openrouter_request_reservation_usd != 0
            or bool(llm_cfg.openrouter_pricing_profile_sha256.strip())
            or llm_cfg.openrouter_durable_cost_ledger
            or any(
                value != -1.0
                for value in (
                    llm_cfg.openrouter_max_price_prompt_usd_per_million,
                    llm_cfg.openrouter_max_price_completion_usd_per_million,
                    llm_cfg.openrouter_max_price_request_usd,
                )
            )
        )
        if campaign_requested:
            # A configured cap is formal campaign mode: partial budget configuration or
            # pinned routing must fail before the first paid request.
            llm_cfg.validate_openrouter_campaign(require_budget=True)
            self._budget = CampaignBudgetLedger(
                llm_cfg.openrouter_budget_ledger,
                cap_usd=llm_cfg.openrouter_campaign_budget_usd,
                reservation_usd=llm_cfg.openrouter_request_reservation_usd,
                pricing_profile_sha256=llm_cfg.openrouter_pricing_profile_sha256,
            )
            self._ledger_executor = ThreadPoolExecutor(
                max_workers=_LEDGER_EXECUTOR_WORKERS,
                thread_name_prefix="tend-ledger",
            )
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                try:
                    self._budget.initialize()
                except BaseException:
                    self._ledger_executor.shutdown(wait=True)
                    self._ledger_executor = None
                    raise
                self._ledger_initialized = True
        self._sem = (
            asyncio.Semaphore(settings.llm.max_concurrency)
            if settings.llm.max_concurrency > 0
            else None
        )
        # Formal budget admission happens before the provider call.  Bound it with
        # a separate semaphore of the same size so queued record tasks do not reserve
        # the entire campaign budget before they are eligible to enter the network.
        # The provider semaphore still spans response streaming; this gate spans the
        # matching reserve-to-settle transaction.
        self._budget_admission_sem = (
            asyncio.BoundedSemaphore(settings.llm.max_concurrency)
            if self._budget is not None and settings.llm.max_concurrency > 0
            else None
        )
        self._transport_reset_lock = asyncio.Lock()
        self._transport_epoch = 0
        self._last_transport_reset_monotonic = 0.0
        self._retired_provider_clients: list[Any] = []
        self._shards: list[_Http2Shard] = []
        self._unhealthy_shards: list[_Http2Shard] = []
        self._shard_lock = asyncio.Lock()
        if not settings.stub:
            self._open_live_provider_client()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close every owned HTTP/2 shard and any retired transports."""
        clients = list(self._retired_provider_clients)
        self._retired_provider_clients = []
        for shard in self._shards:
            if shard.client is not None and shard.client not in clients:
                clients.append(shard.client)
        self._shards = []
        current = self._client
        self._client = None
        if current is not None and current not in clients:
            clients.append(current)
        for client in clients:
            close = getattr(client, "close", None)
            aclose = getattr(client, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass
            elif callable(close):
                result = close()
                if inspect.isawaitable(result):
                    try:
                        await result
                    except Exception:
                        pass
            http_client = getattr(client, "_client", None) or getattr(client, "http_client", None)
            if http_client is not client:
                http_aclose = getattr(http_client, "aclose", None)
                if callable(http_aclose):
                    try:
                        await http_aclose()
                    except Exception:
                        pass
        ledger_executor = self._ledger_executor
        self._ledger_executor = None
        if ledger_executor is not None:
            await asyncio.to_thread(ledger_executor.shutdown, wait=True)

    def _open_live_provider_client(self) -> None:
        """Open one HTTP/2 client bounded to ~80 multiplexed streams."""
        import httpx
        from openai import AsyncOpenAI

        settings = self._s
        # One httpx HTTP/2 connection admits at most 100 concurrent streams; the
        # 101st raises LocalProtocolError. Each shard is therefore one connection
        # (max_connections=1) capped at HTTP2_MAX_STREAMS_PER_CLIENT. Extra
        # shards are opened on demand instead of stuffing more streams onto one
        # connection. Connect stays bounded by the first-token window so a hung
        # handshake fails into the same retry loop.
        ft = settings.llm.first_token_timeout_s
        connect_timeout_s = ft if ft and ft > 0 else settings.llm.timeout_s
        request_timeout = httpx.Timeout(settings.llm.timeout_s, connect=connect_timeout_s)
        http_client = httpx.AsyncClient(
            http2=True,
            limits=httpx.Limits(
                max_connections=1,
                max_keepalive_connections=1,
            ),
            timeout=request_timeout,
        )
        client = AsyncOpenAI(
            base_url=settings.llm.base_url,
            api_key=settings.llm.api_key,
            timeout=request_timeout,
            max_retries=0,
            http_client=http_client,
        )
        self._shards.append(_Http2Shard(client=client))
        self._client = client
        self._transport_epoch += 1

    def _live_provider(self) -> Any:
        client = self._client
        if client is None:
            raise LLMError(
                "provider transport is closed",
                retryable=True,
                context={"status_code": None},
            )
        return client

    def _mark_shard_unhealthy(self, shard: _Http2Shard | None) -> None:
        if shard is None or shard in self._unhealthy_shards:
            return
        shard.retired = True
        self._unhealthy_shards.append(shard)

    def _shard_admits_new_streams(self, item: _Http2Shard) -> bool:
        return (
            not item.retired
            and item not in self._unhealthy_shards
            and item.in_flight < HTTP2_MAX_STREAMS_PER_CLIENT
        )

    @asynccontextmanager
    async def _borrow_live_provider(self):
        """Hold one HTTP/2 stream slot for create() plus stream collection."""
        shard: _Http2Shard | None = None
        injected = self._client
        if injected is not None and all(
            item.client is not injected for item in self._shards
        ):
            # Tests replace ``_client`` with a fake transport after a live
            # ``__init__`` that already opened a real shard. Use the replacement
            # and do not send those calls through the leftover HTTP/2 pool.
            yield injected, None
            return
        if self._shards:
            async with self._shard_lock:
                chosen: _Http2Shard | None = None
                for item in self._shards:
                    if self._shard_admits_new_streams(item):
                        chosen = item
                        break
                if chosen is None:
                    self._open_live_provider_client()
                    chosen = self._shards[-1]
                chosen.in_flight += 1
                shard = chosen
            try:
                yield shard.client, shard
            finally:
                async with self._shard_lock:
                    shard.in_flight = max(0, shard.in_flight - 1)
            return
        raise LLMError(
            "provider transport is closed",
            retryable=True,
            context={"status_code": None},
        )

    def _require_streaming_first_token_watchdog(
        self, *, stream: bool, first_token_timeout_s: float
    ) -> None:
        """Formal campaigns observe first-token arrival on the SSE stream."""
        if self._budget is None:
            return
        if not stream:
            raise CampaignPauseError(
                "formal campaign LLM calls must stream; first-token arrival cannot be observed otherwise",
                context={"pause_reason": "formal_stream_required"},
            )
        if first_token_timeout_s <= 0:
            raise CampaignPauseError(
                "formal campaign LLM calls require a positive first-token timeout",
                context={"pause_reason": "formal_first_token_watchdog_required"},
            )

    @staticmethod
    def _stalled_stream_requires_new_transport(err: LLMError) -> bool:
        return str(err.context.get("timeout_phase") or "") in {
            "response_headers",
            "first_token",
            "transport_closed",
        }

    async def _reset_live_provider_transport(self) -> None:
        """Admit later retries on a new shard; do not aclose in-flight siblings.

        Only the stalled shard is retired from new admissions. Its connection
        stays open until LLMClient.aclose() so multiplexed siblings can finish.
        """
        if self._s.stub:
            return
        async with self._transport_reset_lock:
            now = time.monotonic()
            if now - self._last_transport_reset_monotonic < 1.0:
                return
            async with self._shard_lock:
                victims: list[_Http2Shard] = []
                if self._unhealthy_shards:
                    victims = list(self._unhealthy_shards)
                    self._unhealthy_shards.clear()
                elif self._shards:
                    current = self._client
                    victims = [item for item in self._shards if item.client is current]
                elif self._client is not None:
                    self._retired_provider_clients.append(self._client)
                for shard in victims:
                    shard.retired = True
                    if shard in self._shards:
                        self._shards.remove(shard)
                    if shard.client is not None:
                        self._retired_provider_clients.append(shard.client)
                self._open_live_provider_client()
                self._last_transport_reset_monotonic = now

    def set_stub(self, fn: StubFn) -> None:
        """Register the canned-response function used when ``settings.stub`` is True."""
        self._stub_fn = fn

    # ------------------------------------------------------------------ #
    async def complete(
        self,
        *,
        agent: str,
        messages: list[Message],
        logger: RunLogger | None = None,
        task_logger: "TaskLogger | None" = None,
        schema: dict | None = None,
        expect_json: bool | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        thinking: str | None = None,
        stream: bool | None = None,
        first_token_timeout_s: float | None = None,
        omit_max_tokens: bool = False,
        json_repair_retries: int = 2,
    ) -> LLMResult:
        """Run one logical completion, returning a validated :class:`LLMResult`.

        Raises a typed :class:`~tend.errors.LLMError` (already logged as an anomaly with a
        ``transcript_ref``) if the call cannot be completed within the retry budgets.

        When ``task_logger`` is given, the call is logged DynaDB-style through the
        TaskLogger protocol (``llm/<call_id>.md`` + ``cost_summary.jsonl``) and the
        legacy RunLogger transcript path is fully bypassed.
        """
        log = (logger or self._log).bind(agent=agent)
        call_id = task_logger.new_llm_call_id() if task_logger is not None else uuid4().hex[:12]
        model = model or self._s.llm.model
        temperature = (
            None
            if self._s.llm.omit_temperature
            else (self._s.llm.temperature if temperature is None else temperature)
        )
        omit_effective_max_tokens = omit_max_tokens or self._s.llm.omit_max_tokens
        max_tokens = (
            max_tokens or self._s.llm.max_tokens
            if self._s.llm.force_max_tokens or not omit_effective_max_tokens
            else None
        )
        reasoning_effort = reasoning_effort or self._s.llm.reasoning_effort
        thinking = thinking or self._s.llm.thinking
        stream = self._s.llm.stream if stream is None else stream
        first_token_timeout_s = (
            self._s.llm.first_token_timeout_s
            if first_token_timeout_s is None
            else first_token_timeout_s
        )
        self._require_streaming_first_token_watchdog(
            stream=bool(stream), first_token_timeout_s=float(first_token_timeout_s)
        )
        expect_json = (schema is not None) if expect_json is None else expect_json

        convo = list(messages)
        attempts: list[dict[str, Any]] = []
        # One truncation budget covers the whole logical call, including later JSON
        # repair sends.  Keeping this state outside the repair loop prevents every
        # repair round from silently re-arming the expensive truncation allowance.
        truncation_state = {"seen": 0}
        t0 = time.monotonic()
        provider_kwargs = self._provider_request_options(
            response_format=response_format,
            reasoning_effort=reasoning_effort,
            thinking=thinking,
        )
        request_config = {
            "provider_base_url": self._s.llm.base_url.rstrip("/"),
            "provider_base_url_sha256": hashlib.sha256(
                self._s.llm.base_url.rstrip("/").encode("utf-8")
            ).hexdigest(),
            "temperature": temperature,
            "expect_json": expect_json,
            "schema": schema,
            "json_repair_retries": json_repair_retries,
            "provider_kwargs": provider_kwargs,
            "stream": stream,
            "first_token_timeout_s": first_token_timeout_s,
            "campaign_budget": self._budget_request_config(),
        }
        if max_tokens is not None:
            request_config["max_tokens"] = max_tokens
        try:
            if task_logger is not None:
                start_ref = ""
                start_diagnostics_ref = ""
                task_logger.log_llm_request(
                    call_id,
                    model=model,
                    messages=convo,
                    tools=None,
                    temperature=temperature,
                    response_format=response_format,
                    agent=agent,
                    expect_json=expect_json,
                    stream=stream,
                )
            else:
                prompt_chars = sum(
                    len(str(m.get("content", ""))) if isinstance(m, dict) else 0 for m in convo
                )
                start_ref = log.save_transcript(
                    agent,
                    call_id,
                    {
                        "model": model,
                        **request_config,
                        "messages": convo,
                        "attempts": attempts,
                        "started": True,
                    },
                )
                start_diagnostics_ref = _llm_diagnostics_ref(
                    log, agent, call_id, transcript_ref=start_ref
                )
                log.info(
                    "llm_call_start",
                    agent=agent,
                    call_id=call_id,
                    model=model,
                    message_count=len(convo),
                    prompt_chars=prompt_chars,
                    transcript_ref=start_ref,
                    diagnostics_ref=start_diagnostics_ref,
                )
            # prompt validation is inside the try so prompt anomalies are captured too
            self._validate_prompt(messages, agent, call_id)
            for repair in range(json_repair_retries + 1):
                text, finish, usage = await self._send_with_transport_retries(
                    agent,
                    call_id,
                    model,
                    convo,
                    temperature,
                    max_tokens,
                    provider_kwargs,
                    stream,
                    first_token_timeout_s,
                    attempts,
                    log,
                    transcript_ref=start_ref,
                    diagnostics_ref=start_diagnostics_ref,
                    task_logger=task_logger,
                    request_config=request_config,
                    repair_index=repair,
                    truncation_state=truncation_state,
                )
                if not expect_json:
                    if task_logger is not None:
                        await self._task_logger_settle_received_attempt(
                            task_logger,
                            agent=agent,
                            call_id=call_id,
                            model=model,
                            repair_index=repair,
                            call_status="success",
                            attempts=attempts,
                            request_config=request_config,
                        )
                        self._compact_attempts(attempts)
                    return self._finish(
                        agent,
                        call_id,
                        model,
                        text,
                        None,
                        finish,
                        usage,
                        t0,
                        attempts,
                        log,
                        messages=convo,
                        request_config=request_config,
                        task_logger=task_logger,
                    )
                try:
                    parsed = _extract_json(text)
                    if schema is not None:
                        errs = _schema_errors(parsed, schema)
                        if errs:
                            raise SchemaValidationError(
                                "output failed schema validation",
                                context={"violations": errs},
                            )
                    if task_logger is not None:
                        await self._task_logger_settle_received_attempt(
                            task_logger,
                            agent=agent,
                            call_id=call_id,
                            model=model,
                            repair_index=repair,
                            call_status="success",
                            attempts=attempts,
                            request_config=request_config,
                        )
                        self._compact_attempts(attempts)
                    return self._finish(
                        agent,
                        call_id,
                        model,
                        text,
                        parsed,
                        finish,
                        usage,
                        t0,
                        attempts,
                        log,
                        messages=convo,
                        request_config=request_config,
                        task_logger=task_logger,
                    )
                except (ResponseParseError, SchemaValidationError) as verr:
                    attempts[-1]["validation_error"] = verr.to_record()
                    will_repair = repair < json_repair_retries
                    if task_logger is not None:
                        await self._task_logger_settle_received_attempt(
                            task_logger,
                            agent=agent,
                            call_id=call_id,
                            model=model,
                            repair_index=repair,
                            call_status="retry" if will_repair else "error",
                            attempts=attempts,
                            request_config=request_config,
                            retry_kind="json_repair" if will_repair else None,
                            error=verr,
                            failure_phase="structured_output_validation",
                        )
                        self._compact_attempts(attempts)
                    if not will_repair:
                        raise
                    convo = convo + [
                        {"role": "assistant", "content": text},
                        {"role": "user", "content": self._repair_prompt(verr, schema)},
                    ]
                    if task_logger is not None:
                        raw_response = attempts[-1].get("raw_response")
                        task_logger.warning(
                            "llm_repair_retry",
                            agent=agent,
                            call_id=call_id,
                            attempt=repair + 1,
                            reason=verr.anomaly.value,
                            validation_error=verr.to_record(),
                            attempt_diagnostics={
                                "finish_reason": attempts[-1].get("finish_reason"),
                                "usage": attempts[-1].get("usage"),
                                "latency_s": attempts[-1].get("latency_s"),
                                "response_char_count": len(text),
                                "provider_metadata": attempts[-1].get("provider_metadata"),
                                "stream_chunk_count": (
                                    raw_response.get("stream_chunk_count")
                                    if isinstance(raw_response, dict)
                                    else None
                                ),
                                "reasoning_char_count": (
                                    raw_response.get("reasoning_char_count")
                                    if isinstance(raw_response, dict)
                                    else None
                                ),
                                "content_char_count": (
                                    raw_response.get("content_char_count")
                                    if isinstance(raw_response, dict)
                                    else None
                                ),
                            },
                            request_max_tokens=max_tokens,
                        )
                    else:
                        log.warning(
                            "llm_repair_retry",
                            agent=agent,
                            call_id=call_id,
                            attempt=repair + 1,
                            reason=verr.anomaly.value,
                            transcript_ref=start_ref,
                            diagnostics_ref=start_diagnostics_ref,
                        )
            raise LLMError("exhausted repair retries", context={"agent": agent})  # unreachable
        except LLMError as err:
            err = await self._pause_formal_campaign_error(err)
            if task_logger is not None:
                ref = self._task_logger_log_error(
                    task_logger,
                    call_id,
                    model,
                    attempts=attempts,
                    request_config=request_config,
                    error=err,
                )
                diagnostics_ref = ref
            else:
                ref = log.save_transcript(
                    agent,
                    call_id,
                    {
                        "model": model,
                        **request_config,
                        "messages": convo,
                        "attempts": attempts,
                        "failed": True,
                        "error": err.to_record(),
                    },
                )
                diagnostics_ref = _llm_diagnostics_ref(log, agent, call_id, transcript_ref=ref)
            err.with_context(
                agent=agent,
                call_id=call_id,
                model=model,
                transcript_ref=ref,
                diagnostics_ref=diagnostics_ref,
            )
            log.anomaly(
                err,
                transcript_ref=ref,
                diagnostics_ref=diagnostics_ref,
                call_id=call_id,
                task_id=(
                    getattr(task_logger, "task_id", None) if task_logger is not None else None
                ),
                stage=(getattr(task_logger, "stage", None) if task_logger is not None else None),
            )
            raise err
        except Exception as exc:  # noqa: BLE001 - preserve prompt context for LLM-layer bugs
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            if task_logger is not None:
                ref = self._task_logger_log_error(
                    task_logger,
                    call_id,
                    model,
                    attempts=attempts,
                    request_config=request_config,
                    error=exc,
                )
            else:
                ref = log.save_transcript(
                    agent,
                    call_id,
                    {
                        "model": model,
                        **request_config,
                        "messages": convo,
                        "attempts": attempts,
                        "failed": True,
                        "unexpected_exception": {
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc),
                            "traceback": tb,
                        },
                    },
                )
            err = LLMError(
                f"unexpected LLM client error: {type(exc).__name__}: {exc}",
                anomaly=Anomaly.INTERNAL,
                context={
                    "agent": agent,
                    "call_id": call_id,
                    "model": model,
                    "transcript_ref": ref,
                    "diagnostics_ref": _llm_diagnostics_ref(
                        log, agent, call_id, transcript_ref=ref
                    ),
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "traceback": tb,
                },
            )
            err = await self._pause_formal_campaign_error(err)
            log.anomaly(
                err,
                transcript_ref=ref,
                diagnostics_ref=_llm_diagnostics_ref(log, agent, call_id, transcript_ref=ref),
                call_id=call_id,
                task_id=(
                    getattr(task_logger, "task_id", None) if task_logger is not None else None
                ),
                stage=(getattr(task_logger, "stage", None) if task_logger is not None else None),
            )
            raise err from exc

    # ------------------------------------------------------------------ #
    async def complete_with_tools(
        self,
        *,
        agent: str,
        messages: list[Message],
        tools: list[ToolSchema],
        logger: RunLogger | None = None,
        tool_choice: ToolChoice | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool | None = None,
        first_token_timeout_s: float | None = None,
    ) -> ToolLLMResult:
        """Run one provider-native tool-call completion.

        This is intentionally separate from :meth:`complete`: JSON parsing/schema repair
        remains the structured-output contract, while SMART-EG can use native provider
        tool calls with the same transcript, retry, and anomaly behavior.
        """
        log = (logger or self._log).bind(agent=agent)
        call_id = uuid4().hex[:12]
        model = model or self._s.llm.model
        temperature = (
            None
            if self._s.llm.omit_temperature
            else (self._s.llm.temperature if temperature is None else temperature)
        )
        max_tokens = (
            max_tokens or self._s.llm.max_tokens
            if self._s.llm.force_max_tokens or not self._s.llm.omit_max_tokens
            else None
        )
        stream = self._s.llm.stream if stream is None else stream
        first_token_timeout_s = (
            self._s.llm.first_token_timeout_s
            if first_token_timeout_s is None
            else first_token_timeout_s
        )
        self._require_streaming_first_token_watchdog(
            stream=bool(stream), first_token_timeout_s=float(first_token_timeout_s)
        )
        provider_kwargs = self._tool_provider_request_options(model)
        requested_tool_choice = tool_choice
        tool_choice_disabled_for_model = False
        tool_choice_disabled_reason = None
        if tool_choice is not None:
            tool_choice_disabled_reason = self._tool_choice_disabled_reason(model)
            if tool_choice_disabled_reason:
                tool_choice = None
                tool_choice_disabled_for_model = True

        convo = list(messages)
        attempts: list[dict[str, Any]] = []
        t0 = time.monotonic()
        request_config = {
            "provider_base_url": self._s.llm.base_url.rstrip("/"),
            "provider_base_url_sha256": hashlib.sha256(
                self._s.llm.base_url.rstrip("/").encode("utf-8")
            ).hexdigest(),
            "temperature": temperature,
            "tools": tools,
            "tool_choice": tool_choice,
            "requested_tool_choice": requested_tool_choice,
            "tool_choice_disabled_for_model": tool_choice_disabled_for_model,
            "tool_choice_disabled_reason": tool_choice_disabled_reason,
            "provider_kwargs": provider_kwargs,
            "stream": stream,
            "first_token_timeout_s": first_token_timeout_s,
            "campaign_budget": self._budget_request_config(),
        }
        if max_tokens is not None:
            request_config["max_tokens"] = max_tokens
        try:
            prompt_chars = sum(
                len(str(m.get("content", ""))) if isinstance(m, dict) else 0 for m in convo
            )
            start_ref = log.save_transcript(
                agent,
                call_id,
                {
                    "model": model,
                    **request_config,
                    "messages": convo,
                    "attempts": attempts,
                    "started": True,
                },
            )
            start_diagnostics_ref = _llm_diagnostics_ref(
                log, agent, call_id, transcript_ref=start_ref
            )
            log.info(
                "llm_call_start",
                agent=agent,
                call_id=call_id,
                model=model,
                message_count=len(convo),
                prompt_chars=prompt_chars,
                tools_count=len(tools),
                stream=stream,
                first_token_timeout_s=first_token_timeout_s,
                transcript_ref=start_ref,
                diagnostics_ref=start_diagnostics_ref,
            )
            self._validate_prompt(convo, agent, call_id)
            self._validate_tools(tools, tool_choice, agent, call_id)
            text, finish, usage, raw, tool_calls, fallback = await self._send_tools_with_retries(
                agent,
                call_id,
                model,
                convo,
                temperature,
                max_tokens,
                tools,
                tool_choice,
                provider_kwargs,
                stream,
                first_token_timeout_s,
                attempts,
                log,
                transcript_ref=start_ref,
                diagnostics_ref=start_diagnostics_ref,
            )
            return self._finish_tools(
                agent,
                call_id,
                model,
                text,
                finish,
                usage,
                raw,
                tool_calls,
                t0,
                attempts,
                log,
                messages=convo,
                request_config=request_config,
                tool_choice_fallback=fallback,
            )
        except LLMError as err:
            err = await self._pause_formal_campaign_error(err)
            ref = log.save_transcript(
                agent,
                call_id,
                {
                    "model": model,
                    **request_config,
                    "messages": convo,
                    "attempts": attempts,
                    "failed": True,
                    "error": err.to_record(),
                },
            )
            diagnostics_ref = _llm_diagnostics_ref(log, agent, call_id, transcript_ref=ref)
            err.with_context(
                agent=agent,
                call_id=call_id,
                model=model,
                transcript_ref=ref,
                diagnostics_ref=diagnostics_ref,
            )
            log.anomaly(
                err,
                transcript_ref=ref,
                diagnostics_ref=diagnostics_ref,
                call_id=call_id,
            )
            raise err
        except Exception as exc:  # noqa: BLE001 - preserve prompt context for LLM-layer bugs
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            ref = log.save_transcript(
                agent,
                call_id,
                {
                    "model": model,
                    **request_config,
                    "messages": convo,
                    "attempts": attempts,
                    "failed": True,
                    "unexpected_exception": {
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                        "traceback": tb,
                    },
                },
            )
            err = LLMError(
                f"unexpected LLM tool client error: {type(exc).__name__}: {exc}",
                anomaly=Anomaly.INTERNAL,
                context={
                    "agent": agent,
                    "call_id": call_id,
                    "model": model,
                    "transcript_ref": ref,
                    "diagnostics_ref": _llm_diagnostics_ref(
                        log, agent, call_id, transcript_ref=ref
                    ),
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "traceback": tb,
                },
            )
            err = await self._pause_formal_campaign_error(err)
            log.anomaly(
                err,
                transcript_ref=ref,
                diagnostics_ref=_llm_diagnostics_ref(log, agent, call_id, transcript_ref=ref),
                call_id=call_id,
            )
            raise err from exc

    # ------------------------------------------------------------------ #
    def _validate_prompt(self, messages: list[Message], agent: str, call_id: str) -> None:
        if not messages:
            raise PromptAnomalyError("empty message list", context={"agent": agent})
        for i, m in enumerate(messages):
            if not isinstance(m, dict):
                raise PromptAnomalyError(
                    "message is not an object",
                    context={
                        "agent": agent,
                        "call_id": call_id,
                        "index": i,
                        "message_type": type(m).__name__,
                    },
                )
            role = m.get("role")
            allows_empty_assistant_content = role == "assistant" and bool(m.get("tool_calls"))
            if "role" not in m or ("content" not in m and not allows_empty_assistant_content):
                raise PromptAnomalyError(
                    "message missing role/content",
                    context={"agent": agent, "index": i, "keys": list(m)},
                )
            if role not in _ALLOWED_ROLES:
                raise PromptAnomalyError(
                    "message role is not supported",
                    context={
                        "agent": agent,
                        "call_id": call_id,
                        "index": i,
                        "role": role,
                        "allowed_roles": sorted(_ALLOWED_ROLES),
                    },
                )
            extra_keys = sorted(set(m) - _ALLOWED_MESSAGE_KEYS)
            if extra_keys:
                raise PromptAnomalyError(
                    "message contains unsupported fields",
                    context={
                        "agent": agent,
                        "call_id": call_id,
                        "index": i,
                        "keys": list(m),
                        "unsupported_keys": extra_keys,
                    },
                )
            content = m.get("content", "")
            if (
                not isinstance(content, str) or not content.strip()
            ) and not allows_empty_assistant_content:
                raise PromptAnomalyError(
                    "message content empty or non-string",
                    context={
                        "agent": agent,
                        "call_id": call_id,
                        "index": i,
                        "role": m.get("role"),
                    },
                )
        self._validate_tool_message_pairs(messages, agent, call_id)

    def _validate_tools(
        self,
        tools: list[ToolSchema],
        tool_choice: ToolChoice | None,
        agent: str,
        call_id: str,
    ) -> None:
        if not isinstance(tools, list) or not tools:
            raise PromptAnomalyError(
                "tools must be a non-empty list",
                context={"agent": agent, "call_id": call_id, "tools_type": type(tools).__name__},
            )
        names: set[str] = set()
        for i, tool in enumerate(tools):
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise PromptAnomalyError(
                    "tool schema must be an OpenAI function tool",
                    context={"agent": agent, "call_id": call_id, "index": i, "tool": tool},
                )
            function = tool.get("function")
            if not isinstance(function, dict):
                raise PromptAnomalyError(
                    "tool schema missing function object",
                    context={"agent": agent, "call_id": call_id, "index": i},
                )
            name = function.get("name")
            parameters = function.get("parameters")
            if not isinstance(name, str) or not name.strip():
                raise PromptAnomalyError(
                    "tool function missing name",
                    context={"agent": agent, "call_id": call_id, "index": i},
                )
            if parameters is not None and not isinstance(parameters, dict):
                raise PromptAnomalyError(
                    "tool function parameters must be a JSON schema object",
                    context={"agent": agent, "call_id": call_id, "index": i, "name": name},
                )
            names.add(name)
        if tool_choice is None or isinstance(tool_choice, str):
            return
        if not isinstance(tool_choice, dict):
            raise PromptAnomalyError(
                "tool_choice must be a string or OpenAI tool choice object",
                context={
                    "agent": agent,
                    "call_id": call_id,
                    "tool_choice_type": type(tool_choice).__name__,
                },
            )
        choice_function = tool_choice.get("function")
        choice_name = choice_function.get("name") if isinstance(choice_function, dict) else None
        if tool_choice.get("type") != "function" or not isinstance(choice_name, str):
            raise PromptAnomalyError(
                "tool_choice must name a function tool",
                context={"agent": agent, "call_id": call_id, "tool_choice": tool_choice},
            )
        if choice_name not in names:
            raise PromptAnomalyError(
                "tool_choice names an unknown tool",
                context={
                    "agent": agent,
                    "call_id": call_id,
                    "tool_choice": tool_choice,
                    "available_tools": sorted(names),
                },
            )

    def _validate_tool_message_pairs(
        self,
        messages: list[Message],
        agent: str,
        call_id: str,
    ) -> None:
        pending: set[str] = set()
        for i, message in enumerate(messages):
            role = message.get("role")
            if role == "assistant":
                if pending:
                    raise PromptAnomalyError(
                        "assistant tool_calls missing tool result messages",
                        context={
                            "agent": agent,
                            "call_id": call_id,
                            "index": i,
                            "missing_tool_call_ids": sorted(pending),
                        },
                    )
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    continue
                if not isinstance(tool_calls, list):
                    raise PromptAnomalyError(
                        "assistant tool_calls must be a list",
                        context={"agent": agent, "call_id": call_id, "index": i},
                    )
                pending = set()
                for j, call in enumerate(tool_calls):
                    if not isinstance(call, dict):
                        raise PromptAnomalyError(
                            "assistant tool_call must be an object",
                            context={
                                "agent": agent,
                                "call_id": call_id,
                                "index": i,
                                "tool_call_index": j,
                            },
                        )
                    call_id_value = call.get("id")
                    function = call.get("function")
                    if (
                        not isinstance(call_id_value, str)
                        or not call_id_value.strip()
                        or call.get("type") != "function"
                        or not isinstance(function, dict)
                        or not isinstance(function.get("name"), str)
                        or not isinstance(function.get("arguments"), str)
                    ):
                        raise PromptAnomalyError(
                            "assistant tool_call is not OpenAI-compatible",
                            context={
                                "agent": agent,
                                "call_id": call_id,
                                "index": i,
                                "tool_call_index": j,
                            },
                        )
                    pending.add(call_id_value)
                continue
            if role == "tool":
                tool_call_id = message.get("tool_call_id")
                if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                    raise PromptAnomalyError(
                        "tool result message missing tool_call_id",
                        context={"agent": agent, "call_id": call_id, "index": i},
                    )
                if tool_call_id not in pending:
                    raise PromptAnomalyError(
                        "tool message has no matching assistant tool_call",
                        context={
                            "agent": agent,
                            "call_id": call_id,
                            "index": i,
                            "tool_call_id": tool_call_id,
                            "pending_tool_call_ids": sorted(pending),
                        },
                    )
                pending.remove(tool_call_id)
                continue
            if pending:
                raise PromptAnomalyError(
                    "assistant tool_calls missing tool result messages",
                    context={
                        "agent": agent,
                        "call_id": call_id,
                        "index": i,
                        "missing_tool_call_ids": sorted(pending),
                    },
                )
        if pending:
            raise PromptAnomalyError(
                "assistant tool_calls missing tool result messages",
                context={
                    "agent": agent,
                    "call_id": call_id,
                    "missing_tool_call_ids": sorted(pending),
                },
            )

    async def _send_with_transport_retries(
        self,
        agent: str,
        call_id: str,
        model: str,
        convo: list[Message],
        temperature: float | None,
        max_tokens: int | None,
        provider_kwargs: dict[str, Any],
        stream: bool,
        first_token_timeout_s: float,
        attempts: list[dict[str, Any]],
        log: RunLogger,
        *,
        transcript_ref: str,
        diagnostics_ref: str,
        task_logger: "TaskLogger | None" = None,
        request_config: dict[str, Any],
        repair_index: int,
        truncation_state: dict[str, int],
    ) -> tuple[str, str | None, dict[str, int]]:
        attempt = 0
        while True:
            provider_attempt_index = self._next_provider_attempt_index(attempts)
            started_attempt_receipt = self._attempt_receipt_template(
                agent=agent,
                call_id=call_id,
                model=model,
                provider_attempt_index=provider_attempt_index,
                transport_attempt=attempt + 1,
                repair_index=repair_index,
                status="provider_request_started",
                request_config=request_config,
                task_logger=task_logger,
                log=log,
            )
            reservation_id, budget_reserved = await self._reserve_provider_budget(
                call_id,
                provider_attempt_index,
                attempt_receipt=started_attempt_receipt,
            )
            budget_settled = False
            budget_settlement: dict[str, Any] | None = None
            t0 = time.monotonic()
            provider_attempt: dict[str, Any] | None = None
            try:
                text, finish, usage, raw = await self._raw_call(
                    agent,
                    model,
                    convo,
                    temperature,
                    max_tokens,
                    provider_kwargs,
                    stream,
                    first_token_timeout_s,
                )
                provider_metadata = _provider_metadata(raw, finish)
                raw_provider_cost = self._raw_provider_cost(provider_metadata)
                provider_attempt = {
                    "attempt": attempt,
                    "provider_attempt_index": provider_attempt_index,
                    "repair_index": repair_index,
                    "kind": "send",
                    "finish_reason": finish,
                    "usage": usage,
                    "latency_s": round(time.monotonic() - t0, 3),
                    "response": text,
                    "response_preview": text[:500],
                    "provider_kwargs": provider_kwargs,
                    "stream": stream,
                    "first_token_timeout_s": first_token_timeout_s,
                    "provider_metadata": provider_metadata,
                    "provider_cost_observed": _json_safe(raw_provider_cost),
                    "raw_response": _json_safe(raw),
                    "budget_reservation": budget_reserved,
                    "budget_settlement": None,
                }
                attempts.append(provider_attempt)
                # Capture the complete provider response before settlement. If cost
                # validation or another worker's pause stops the campaign, the except
                # path can still durably write response_received=true with raw evidence.
                budget_settled = True
                budget_settlement = await self._settle_provider_budget(
                    reservation_id,
                    known_cost_usd=raw_provider_cost,
                    call_id=call_id,
                    provider_attempt_index=provider_attempt_index,
                    settlement_kind="provider_response",
                    pause_reason=(
                        "provider_cost_missing_or_invalid"
                        if self._provider_cost_usd(provider_metadata) is None
                        and (
                            raw_provider_cost is not None
                            or bool(str(text or "").strip())
                        )
                        else None
                    ),
                    raise_on_pause=False,
                    attempt_receipt=self._attempt_receipt_template(
                        agent=agent,
                        call_id=call_id,
                        model=model,
                        provider_attempt_index=provider_attempt_index,
                        transport_attempt=attempt + 1,
                        repair_index=repair_index,
                        status="response_received_pending_validation",
                        request_config=request_config,
                        task_logger=task_logger,
                        log=log,
                        provider_attempt=provider_attempt,
                        latency_s=float(provider_attempt["latency_s"]),
                        budget_reservation=budget_reserved,
                    ),
                )
                provider_attempt["budget_settlement"] = budget_settlement
                if budget_settlement and budget_settlement.get(
                    "settlement_pause_triggered"
                ):
                    raise CampaignPauseError(
                        "formal OpenRouter campaign paused while settling provider response",
                        context={
                            "pause_reason": budget_settlement.get("pause_reason"),
                            "provider_attempt_index": provider_attempt_index,
                        },
                    )
                self._check_response(text, finish, agent, provider_metadata=provider_metadata)
                if task_logger is None and self._budget is not None:
                    attempt_row_persisted = self._log_run_provider_attempt(
                        log,
                        agent=agent,
                        call_id=call_id,
                        model=model,
                        provider_attempt_index=provider_attempt_index,
                        transport_attempt=attempt + 1,
                        provider_metadata=provider_metadata,
                        response_received=True,
                        latency_s=float(provider_attempt["latency_s"]),
                        usage=usage,
                        provider_cost_observed=provider_attempt.get(
                            "provider_cost_observed"
                        ),
                        error=None,
                        call_status="success",
                        request_config=request_config,
                        budget_reservation=budget_reserved,
                        budget_settlement=budget_settlement,
                    )
                    if attempt_row_persisted:
                        await self._ack_provider_attempt_receipt(budget_settlement)
                return text, finish, usage
            except LLMError as err:
                formal_pause_reason = self._formal_pause_reason(err)
                attempt_latency_s = (
                    float(provider_attempt["latency_s"])
                    if provider_attempt is not None
                    else round(time.monotonic() - t0, 3)
                )
                if not budget_settled:
                    # Explicit 401/402/403/429 rejections and retryable transport/5xx
                    # failures settle at known $0 so the campaign can retry another
                    # route without consuming the full reservation as unknown spend.
                    budget_settled = True
                    budget_settlement = await self._settle_provider_budget(
                        reservation_id,
                        known_cost_usd=self._settled_provider_error_cost(
                            err, provider_attempt
                        ),
                        call_id=call_id,
                        provider_attempt_index=provider_attempt_index,
                        settlement_kind="provider_error",
                        pause_reason=formal_pause_reason,
                        raise_on_pause=False,
                        attempt_receipt=self._attempt_receipt_template(
                            agent=agent,
                            call_id=call_id,
                            model=model,
                            provider_attempt_index=provider_attempt_index,
                            transport_attempt=attempt + 1,
                            repair_index=repair_index,
                            status="provider_error_pending_retry_decision",
                            request_config=request_config,
                            task_logger=task_logger,
                            log=log,
                            error=err,
                            latency_s=attempt_latency_s,
                            budget_reservation=budget_reserved,
                        ),
                    )
                if budget_settlement and budget_settlement.get(
                    "settlement_pause_triggered"
                ):
                    formal_pause_reason = str(
                        budget_settlement.get("pause_reason") or formal_pause_reason
                    )
                if formal_pause_reason:
                    err = self._campaign_pause_from_error(err, formal_pause_reason)
                raw_response = (
                    provider_attempt.get("raw_response") if provider_attempt is not None else None
                )
                attempt_diagnostics = {
                    "response_received": provider_attempt is not None,
                    "latency_s": attempt_latency_s,
                    "finish_reason": (
                        provider_attempt.get("finish_reason")
                        if provider_attempt is not None
                        else None
                    ),
                    "usage": (
                        provider_attempt.get("usage") if provider_attempt is not None else None
                    ),
                    "response_char_count": (
                        len(str(provider_attempt.get("response") or ""))
                        if provider_attempt is not None
                        else 0
                    ),
                    "provider_metadata": (
                        provider_attempt.get("provider_metadata")
                        if provider_attempt is not None
                        else None
                    ),
                    "stream_chunk_count": (
                        raw_response.get("stream_chunk_count")
                        if isinstance(raw_response, dict)
                        else None
                    ),
                    "reasoning_char_count": (
                        raw_response.get("reasoning_char_count")
                        if isinstance(raw_response, dict)
                        else None
                    ),
                    "content_char_count": (
                        raw_response.get("content_char_count")
                        if isinstance(raw_response, dict)
                        else None
                    ),
                    "stream_ended_before_first_token": (
                        raw_response.get("stream_ended_before_first_token")
                        if isinstance(raw_response, dict)
                        else None
                    ),
                    "stream_chunk_samples": (
                        raw_response.get("stream_chunk_samples")
                        if isinstance(raw_response, dict)
                        else None
                    ),
                    "budget_reservation": budget_reserved,
                    "budget_settlement": budget_settlement,
                }
                attempts.append(
                    {
                        "attempt": attempt,
                        "provider_attempt_index": provider_attempt_index,
                        "repair_index": repair_index,
                        "kind": "send_error",
                        "latency_s": attempt_latency_s,
                        "error": err.to_record(),
                        "attempt_diagnostics": attempt_diagnostics,
                        "stream": stream,
                        "first_token_timeout_s": first_token_timeout_s,
                    }
                )
                will_retry = err.retryable and not self._retries_exhausted(attempt)
                if isinstance(err, TruncatedResponseError):
                    truncation_state["seen"] = int(truncation_state.get("seen", 0)) + 1
                    if truncation_state["seen"] > max(0, self._s.llm.max_truncation_retries):
                        will_retry = False
                delay = self._provider_retry_delay(attempt + 1, err)
                if task_logger is not None:
                    self._task_logger_log_attempt(
                        task_logger,
                        agent=agent,
                        call_id=call_id,
                        model=model,
                        provider_attempt_index=provider_attempt_index,
                        transport_attempt=attempt + 1,
                        repair_index=repair_index,
                        call_status="retry" if will_retry else "error",
                        provider_attempt=provider_attempt,
                        request_config=request_config,
                        retry_kind="transport" if will_retry else None,
                        error=err,
                        failure_phase="transport_validation",
                        latency_s=attempt_latency_s,
                    )
                    await self._ack_provider_attempt_receipt(budget_settlement)
                    self._compact_attempts(attempts)
                else:
                    attempt_row_persisted = self._log_run_provider_attempt(
                        log,
                        agent=agent,
                        call_id=call_id,
                        model=model,
                        provider_attempt_index=provider_attempt_index,
                        transport_attempt=attempt + 1,
                        provider_metadata=(
                            provider_attempt.get("provider_metadata")
                            if provider_attempt is not None
                            else None
                        ),
                        response_received=provider_attempt is not None,
                        latency_s=attempt_latency_s,
                        usage=(
                            provider_attempt.get("usage")
                            if provider_attempt is not None
                            else None
                        ),
                        provider_cost_observed=(
                            provider_attempt.get("provider_cost_observed")
                            if provider_attempt is not None
                            else None
                        ),
                        error=err,
                        request_config=request_config,
                        budget_reservation=budget_reserved,
                        budget_settlement=budget_settlement,
                    )
                    if attempt_row_persisted:
                        await self._ack_provider_attempt_receipt(budget_settlement)
                    self._compact_attempts(attempts)
                event = "llm_transport_retry" if will_retry else "llm_transport_terminal_failure"
                if task_logger is not None:
                    task_logger.warning(
                        event,
                        agent=agent,
                        call_id=call_id,
                        attempt=attempt,
                        anomaly=err.anomaly.value if err.anomaly else None,
                        error=err.to_record(),
                        attempt_diagnostics=attempt_diagnostics,
                        request_max_tokens=max_tokens,
                        delay_s=round(delay, 2) if will_retry else 0.0,
                    )
                else:
                    log.warning(
                        event,
                        agent=agent,
                        call_id=call_id,
                        attempt=attempt,
                        anomaly=err.anomaly.value if err.anomaly else None,
                        error=err.to_record(),
                        attempt_diagnostics=attempt_diagnostics,
                        request_max_tokens=max_tokens,
                        delay_s=round(delay, 2) if will_retry else 0.0,
                        transcript_ref=transcript_ref,
                        diagnostics_ref=diagnostics_ref,
                    )
                if will_retry and self._stalled_stream_requires_new_transport(err):
                    await self._reset_live_provider_transport()
                if not will_retry:
                    raise err
                self._notify_retry_progress(err, attempt + 1, delay)
                # _raw_call owns the semaphore only around the live request/stream and
                # has returned/raised before this point.  No permit is held while a
                # reroutable provider failure waits, so fresh calls cannot starve.
                await asyncio.sleep(delay)
                attempt += 1
            except BaseException as exc:
                # Cancellation/SystemExit after a reservation must never strand an
                # alive-process reservation.  The provider may have accepted the call,
                # so charge it as unknown and pause atomically before propagating.
                pause_reason = (
                    "provider_attempt_cancelled"
                    if isinstance(exc, asyncio.CancelledError)
                    else "llm_client_internal_error"
                    if isinstance(exc, Exception)
                    else "provider_attempt_aborted"
                )
                abort_error = CampaignPauseError(
                    "provider attempt ended before normal completion",
                    context={
                        "pause_reason": pause_reason,
                        "provider_attempt_index": provider_attempt_index,
                        "source_exception_type": type(exc).__name__,
                        "source_exception_message": str(exc),
                    },
                )
                attempt_latency_s = (
                    float(provider_attempt["latency_s"])
                    if provider_attempt is not None
                    else round(time.monotonic() - t0, 3)
                )
                if not budget_settled:
                    budget_settled = True
                    budget_settlement = await self._settle_provider_budget(
                        reservation_id,
                        known_cost_usd=None,
                        call_id=call_id,
                        provider_attempt_index=provider_attempt_index,
                        settlement_kind="provider_attempt_aborted",
                        pause_reason=pause_reason,
                        raise_on_pause=False,
                        attempt_receipt=self._attempt_receipt_template(
                            agent=agent,
                            call_id=call_id,
                            model=model,
                            provider_attempt_index=provider_attempt_index,
                            transport_attempt=attempt + 1,
                            repair_index=repair_index,
                            status="error",
                            request_config=request_config,
                            task_logger=task_logger,
                            log=log,
                            error=abort_error,
                            latency_s=attempt_latency_s,
                            budget_reservation=budget_reserved,
                        ),
                    )
                attempts.append(
                    {
                        "attempt": attempt,
                        "provider_attempt_index": provider_attempt_index,
                        "repair_index": repair_index,
                        "kind": "send_error",
                        "latency_s": attempt_latency_s,
                        "error": abort_error.to_record(),
                        "attempt_diagnostics": {
                            "response_received": provider_attempt is not None,
                            "budget_reservation": budget_reserved,
                            "budget_settlement": budget_settlement,
                        },
                    }
                )
                if task_logger is not None:
                    self._task_logger_log_attempt(
                        task_logger,
                        agent=agent,
                        call_id=call_id,
                        model=model,
                        provider_attempt_index=provider_attempt_index,
                        transport_attempt=attempt + 1,
                        repair_index=repair_index,
                        call_status="error",
                        provider_attempt=provider_attempt,
                        request_config=request_config,
                        error=abort_error,
                        failure_phase=pause_reason,
                        budget_reservation=budget_reserved,
                        budget_settlement=budget_settlement,
                        latency_s=attempt_latency_s,
                    )
                    await self._ack_provider_attempt_receipt(budget_settlement)
                else:
                    attempt_row_persisted = self._log_run_provider_attempt(
                        log,
                        agent=agent,
                        call_id=call_id,
                        model=model,
                        provider_attempt_index=provider_attempt_index,
                        transport_attempt=attempt + 1,
                        provider_metadata=(
                            provider_attempt.get("provider_metadata")
                            if provider_attempt is not None
                            else None
                        ),
                        response_received=provider_attempt is not None,
                        latency_s=attempt_latency_s,
                        usage=(
                            provider_attempt.get("usage")
                            if provider_attempt is not None
                            else None
                        ),
                        provider_cost_observed=(
                            provider_attempt.get("provider_cost_observed")
                            if provider_attempt is not None
                            else None
                        ),
                        error=abort_error,
                        request_config=request_config,
                        budget_reservation=budget_reserved,
                        budget_settlement=budget_settlement,
                    )
                    if attempt_row_persisted:
                        await self._ack_provider_attempt_receipt(budget_settlement)
                self._compact_attempts(attempts)
                raise

    async def _send_tools_with_retries(
        self,
        agent: str,
        call_id: str,
        model: str,
        convo: list[Message],
        temperature: float | None,
        max_tokens: int | None,
        tools: list[ToolSchema],
        tool_choice: ToolChoice | None,
        provider_kwargs: dict[str, Any],
        stream: bool,
        first_token_timeout_s: float,
        attempts: list[dict[str, Any]],
        log: RunLogger,
        *,
        transcript_ref: str,
        diagnostics_ref: str,
    ) -> tuple[str, str | None, dict[str, int], Any, list[dict[str, Any]], bool]:
        last: LLMError | None = None
        active_tool_choice = tool_choice
        fallback_used = False
        retries_used = 0
        send_index = 0
        # Same separate truncation budget as the non-tool path; ReAct drives long
        # multi-step traces through here, so an unbounded truncation retry is the most
        # expensive failure mode in the agentic arm.
        truncations_seen = 0
        while True:
            provider_attempt_index = self._next_provider_attempt_index(attempts)
            tool_request_config = {
                "provider_kwargs": provider_kwargs,
                "campaign_budget": self._budget_request_config(),
            }
            started_attempt_receipt = self._attempt_receipt_template(
                agent=agent,
                call_id=call_id,
                model=model,
                provider_attempt_index=provider_attempt_index,
                transport_attempt=send_index + 1,
                repair_index=0,
                status="provider_request_started",
                request_config=tool_request_config,
                log=log,
            )
            reservation_id, budget_reserved = await self._reserve_provider_budget(
                call_id,
                provider_attempt_index,
                attempt_receipt=started_attempt_receipt,
            )
            budget_settled = False
            budget_settlement: dict[str, Any] | None = None
            t0 = time.monotonic()
            provider_metadata: dict[str, Any] | None = None
            provider_attempt: dict[str, Any] | None = None
            try:
                text, finish, usage, raw, tool_calls = await self._raw_tool_call(
                    agent,
                    model,
                    convo,
                    temperature,
                    max_tokens,
                    tools,
                    active_tool_choice,
                    provider_kwargs,
                    stream,
                    first_token_timeout_s,
                )
                provider_metadata = _provider_metadata(raw, finish)
                raw_provider_cost = self._raw_provider_cost(provider_metadata)
                provider_attempt = {
                    "attempt": send_index,
                    "provider_attempt_index": provider_attempt_index,
                    "kind": "tool_send",
                    "finish_reason": finish,
                    "usage": usage,
                    "latency_s": round(time.monotonic() - t0, 3),
                    "response": text,
                    "response_preview": text[:500],
                    "tool_calls": tool_calls,
                    "provider_metadata": provider_metadata,
                    "provider_cost_observed": _json_safe(raw_provider_cost),
                    "raw_response": _json_safe(raw),
                    "stream": stream,
                    "first_token_timeout_s": first_token_timeout_s,
                    "tool_choice": active_tool_choice,
                    "budget_reservation": budget_reserved,
                    "budget_settlement": None,
                }
                attempts.append(provider_attempt)
                budget_settled = True
                budget_settlement = await self._settle_provider_budget(
                    reservation_id,
                    known_cost_usd=raw_provider_cost,
                    call_id=call_id,
                    provider_attempt_index=provider_attempt_index,
                    settlement_kind="tool_provider_response",
                    pause_reason=(
                        "provider_cost_missing_or_invalid"
                        if self._provider_cost_usd(provider_metadata) is None
                        and (
                            raw_provider_cost is not None
                            or bool(str(text or "").strip())
                            or bool(tool_calls)
                        )
                        else None
                    ),
                    raise_on_pause=False,
                    attempt_receipt=self._attempt_receipt_template(
                        agent=agent,
                        call_id=call_id,
                        model=model,
                        provider_attempt_index=provider_attempt_index,
                        transport_attempt=send_index + 1,
                        repair_index=0,
                        status="response_received_pending_validation",
                        request_config=tool_request_config,
                        log=log,
                        provider_attempt=provider_attempt,
                        latency_s=float(provider_attempt["latency_s"]),
                        budget_reservation=budget_reserved,
                    ),
                )
                provider_attempt["budget_settlement"] = budget_settlement
                if budget_settlement and budget_settlement.get(
                    "settlement_pause_triggered"
                ):
                    raise CampaignPauseError(
                        "formal OpenRouter campaign paused while settling tool response",
                        context={
                            "pause_reason": budget_settlement.get("pause_reason"),
                            "provider_attempt_index": provider_attempt_index,
                        },
                    )
                self._check_tool_response(
                    text,
                    tool_calls,
                    finish,
                    agent,
                    provider_metadata=provider_metadata,
                )
                attempt_row_persisted = self._log_run_provider_attempt(
                    log,
                    agent=agent,
                    call_id=call_id,
                    model=model,
                    provider_attempt_index=provider_attempt_index,
                    transport_attempt=send_index + 1,
                    provider_metadata=provider_metadata,
                    response_received=True,
                    latency_s=float(provider_attempt["latency_s"]),
                    usage=usage,
                    provider_cost_observed=provider_attempt.get(
                        "provider_cost_observed"
                    ),
                    error=None,
                    call_status="success",
                    request_config=tool_request_config,
                    budget_reservation=budget_reserved,
                    budget_settlement=budget_settlement,
                )
                if attempt_row_persisted:
                    await self._ack_provider_attempt_receipt(budget_settlement)
                return text, finish, usage, raw, tool_calls, fallback_used
            except LLMError as err:
                last = err
                formal_pause_reason = self._formal_pause_reason(err)
                attempt_latency_s = (
                    float(provider_attempt["latency_s"])
                    if provider_attempt is not None
                    else round(time.monotonic() - t0, 3)
                )
                if not budget_settled:
                    budget_settled = True
                    budget_settlement = await self._settle_provider_budget(
                        reservation_id,
                        known_cost_usd=self._settled_provider_error_cost(
                            err, provider_attempt
                        ),
                        call_id=call_id,
                        provider_attempt_index=provider_attempt_index,
                        settlement_kind="tool_provider_error",
                        pause_reason=formal_pause_reason,
                        raise_on_pause=False,
                        attempt_receipt=self._attempt_receipt_template(
                            agent=agent,
                            call_id=call_id,
                            model=model,
                            provider_attempt_index=provider_attempt_index,
                            transport_attempt=send_index + 1,
                            repair_index=0,
                            status="provider_error_pending_retry_decision",
                            request_config=tool_request_config,
                            log=log,
                            error=err,
                            latency_s=attempt_latency_s,
                            budget_reservation=budget_reserved,
                        ),
                    )
                if budget_settlement and budget_settlement.get(
                    "settlement_pause_triggered"
                ):
                    formal_pause_reason = str(
                        budget_settlement.get("pause_reason") or formal_pause_reason
                    )
                if formal_pause_reason:
                    err = self._campaign_pause_from_error(err, formal_pause_reason)
                    last = err
                attempts.append(
                    {
                        "attempt": send_index,
                        "provider_attempt_index": provider_attempt_index,
                        "kind": "tool_send_error",
                        "latency_s": attempt_latency_s,
                        "error": err.to_record(),
                        "stream": stream,
                        "first_token_timeout_s": first_token_timeout_s,
                        "tool_choice": active_tool_choice,
                        "budget_reservation": budget_reserved,
                        "budget_settlement": budget_settlement,
                    }
                )
                attempt_row_persisted = self._log_run_provider_attempt(
                    log,
                    agent=agent,
                    call_id=call_id,
                    model=model,
                    provider_attempt_index=provider_attempt_index,
                    transport_attempt=send_index + 1,
                    provider_metadata=provider_metadata,
                    response_received=provider_attempt is not None,
                    latency_s=attempt_latency_s,
                    usage=(
                        provider_attempt.get("usage")
                        if provider_attempt is not None
                        else None
                    ),
                    provider_cost_observed=(
                        provider_attempt.get("provider_cost_observed")
                        if provider_attempt is not None
                        else None
                    ),
                    error=err,
                    request_config=tool_request_config,
                    budget_reservation=budget_reserved,
                    budget_settlement=budget_settlement,
                )
                if attempt_row_persisted:
                    await self._ack_provider_attempt_receipt(budget_settlement)
                self._compact_attempts(attempts)
                if isinstance(err, LLMTimeoutError):
                    timeout_phase = str(err.context.get("timeout_phase") or "unknown")
                    timeout_event = {
                        "response_headers": "llm_stream_response_headers_timeout",
                        "first_token": "llm_stream_first_token_timeout",
                        "inter_token": "llm_stream_inter_token_timeout",
                    }.get(timeout_phase, "llm_transport_timeout")
                    log.warning(
                        timeout_event,
                        agent=agent,
                        call_id=call_id,
                        model=model,
                        attempt=send_index,
                        timeout_phase=timeout_phase,
                        first_token_timeout_s=first_token_timeout_s,
                        inter_token_timeout_s=self._s.llm.timeout_s,
                        transcript_ref=transcript_ref,
                        diagnostics_ref=diagnostics_ref,
                    )
                if (
                    active_tool_choice is not None
                    and not fallback_used
                    and self._should_fallback_tool_choice(err)
                ):
                    log.warning(
                        "llm_tool_choice_fallback",
                        agent=agent,
                        call_id=call_id,
                        model=model,
                        requested_tool_choice=active_tool_choice,
                        reason=err.message,
                        transcript_ref=transcript_ref,
                        diagnostics_ref=diagnostics_ref,
                    )
                    self._tool_choice_unsupported_models.add(model)
                    active_tool_choice = None
                    fallback_used = True
                    send_index += 1
                    continue
                if isinstance(err, TruncatedResponseError):
                    truncations_seen += 1
                    if truncations_seen > max(0, self._s.llm.max_truncation_retries):
                        raise err
                if not err.retryable or self._retries_exhausted(retries_used):
                    raise err
                if self._stalled_stream_requires_new_transport(err):
                    await self._reset_live_provider_transport()
                delay = self._provider_retry_delay(retries_used + 1, err)
                log.warning(
                    "llm_transport_retry",
                    agent=agent,
                    call_id=call_id,
                    attempt=send_index,
                    anomaly=err.anomaly.value if err.anomaly else None,
                    delay_s=round(delay, 2),
                    transcript_ref=transcript_ref,
                    diagnostics_ref=diagnostics_ref,
                )
                self._notify_retry_progress(err, retries_used + 1, delay)
                retries_used += 1
                send_index += 1
                await asyncio.sleep(delay)
            except BaseException as exc:
                pause_reason = (
                    "provider_attempt_cancelled"
                    if isinstance(exc, asyncio.CancelledError)
                    else "llm_client_internal_error"
                    if isinstance(exc, Exception)
                    else "provider_attempt_aborted"
                )
                abort_error = CampaignPauseError(
                    "tool provider attempt ended before normal completion",
                    context={
                        "pause_reason": pause_reason,
                        "provider_attempt_index": provider_attempt_index,
                        "source_exception_type": type(exc).__name__,
                        "source_exception_message": str(exc),
                    },
                )
                attempt_latency_s = (
                    float(provider_attempt["latency_s"])
                    if provider_attempt is not None
                    else round(time.monotonic() - t0, 3)
                )
                if not budget_settled:
                    budget_settled = True
                    budget_settlement = await self._settle_provider_budget(
                        reservation_id,
                        known_cost_usd=None,
                        call_id=call_id,
                        provider_attempt_index=provider_attempt_index,
                        settlement_kind="tool_provider_attempt_aborted",
                        pause_reason=pause_reason,
                        raise_on_pause=False,
                        attempt_receipt=self._attempt_receipt_template(
                            agent=agent,
                            call_id=call_id,
                            model=model,
                            provider_attempt_index=provider_attempt_index,
                            transport_attempt=send_index + 1,
                            repair_index=0,
                            status="error",
                            request_config=tool_request_config,
                            log=log,
                            error=abort_error,
                            latency_s=attempt_latency_s,
                            budget_reservation=budget_reserved,
                        ),
                    )
                attempts.append(
                    {
                        "attempt": send_index,
                        "provider_attempt_index": provider_attempt_index,
                        "kind": "tool_send_error",
                        "latency_s": attempt_latency_s,
                        "error": abort_error.to_record(),
                        "budget_reservation": budget_reserved,
                        "budget_settlement": budget_settlement,
                    }
                )
                attempt_row_persisted = self._log_run_provider_attempt(
                    log,
                    agent=agent,
                    call_id=call_id,
                    model=model,
                    provider_attempt_index=provider_attempt_index,
                    transport_attempt=send_index + 1,
                    provider_metadata=(
                        provider_attempt.get("provider_metadata")
                        if provider_attempt is not None
                        else None
                    ),
                    response_received=provider_attempt is not None,
                    latency_s=attempt_latency_s,
                    usage=(
                        provider_attempt.get("usage")
                        if provider_attempt is not None
                        else None
                    ),
                    provider_cost_observed=(
                        provider_attempt.get("provider_cost_observed")
                        if provider_attempt is not None
                        else None
                    ),
                    error=abort_error,
                    request_config=tool_request_config,
                    budget_reservation=budget_reserved,
                    budget_settlement=budget_settlement,
                )
                if attempt_row_persisted:
                    await self._ack_provider_attempt_receipt(budget_settlement)
                self._compact_attempts(attempts)
                raise
        assert last is not None
        raise last

    async def _raw_call(
        self,
        agent: str,
        model: str,
        convo: list[Message],
        temperature: float | None,
        max_tokens: int | None,
        provider_kwargs: dict[str, Any],
        stream: bool,
        first_token_timeout_s: float,
    ) -> tuple[str, str | None, dict[str, int], Any]:
        if self._s.stub:
            return self._stub_call(agent, convo)
        kwargs: dict[str, Any] = {"model": model, "messages": convo, **provider_kwargs}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
        # The semaphore must bound the ENTIRE in-flight call. With stream=True,
        # ``create()`` returns as soon as response HEADERS arrive (<1s), so a semaphore
        # wrapping only the create would release immediately and TEND_LLM_MAX_CONCURRENCY
        # would bound nothing — every queued work item's stream would run at once (the
        # connect-stampede / congestion failure mode observed at scale).
        async with self._sem or nullcontext():
            async with self._borrow_live_provider() as (provider, shard):
                first_token_deadline = (
                    time.monotonic() + first_token_timeout_s
                    if stream and first_token_timeout_s > 0
                    else None
                )
                try:
                    if first_token_deadline is not None:
                        # With stream=True the provider sends response headers on
                        # admission, so create() returning is part of the first-token
                        # contract. Under load the provider also throttles by ACCEPTING
                        # the connection and never answering (observed 2026-06-12:
                        # established conns, zero completions for ~1h, retries cycling
                        # at the 1800s httpx read timeout) — bound the header wait by
                        # the first-token window. Non-stream calls legitimately block
                        # here for the whole generation and stay unbounded.
                        remaining = first_token_deadline - time.monotonic()
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        resp = await asyncio.wait_for(
                            provider.chat.completions.create(**kwargs),
                            timeout=remaining,
                        )
                    else:
                        resp = await provider.chat.completions.create(**kwargs)
                except asyncio.TimeoutError as exc:
                    self._mark_shard_unhealthy(shard)
                    raise LLMTimeoutError(
                        "provider response headers timeout",
                        context={
                            "timeout_phase": "response_headers",
                            "first_token_timeout_s": first_token_timeout_s,
                        },
                    ) from exc
                except LLMError:
                    raise
                except Exception as exc:  # noqa: BLE001 - mapped to typed anomalies below
                    mapped = self._map_provider_error(exc)
                    if self._stalled_stream_requires_new_transport(mapped):
                        self._mark_shard_unhealthy(shard)
                    raise mapped from exc
                if stream:
                    try:
                        return await self._collect_completion_stream(
                            resp,
                            first_token_timeout_s,
                            first_token_deadline,
                        )
                    except LLMError as err:
                        if self._stalled_stream_requires_new_transport(err):
                            self._mark_shard_unhealthy(shard)
                        raise
                    except Exception as exc:  # noqa: BLE001 - streaming iterator faults are provider faults
                        mapped = self._map_provider_error(exc)
                        if self._stalled_stream_requires_new_transport(mapped):
                            self._mark_shard_unhealthy(shard)
                        raise mapped from exc
                    finally:
                        # Always release the SSE stream — an abandoned (timed-out) stream
                        # keeps its pooled connection checked out and the provider keeps
                        # GENERATING (and billing) into a socket nobody reads; under
                        # retry-until-success that snowballs into self-inflicted load.
                        await self._close_stream(resp)
        choice = resp.choices[0]
        text = choice.message.content or ""
        usage = (
            {
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
                "total_tokens": getattr(resp.usage, "total_tokens", 0),
            }
            if resp.usage
            else {}
        )
        # reasoning models (deepseek-v4-flash) return chain-of-thought separately; capture it
        # for the transcript so anomalies can be diagnosed against the model's actual reasoning
        reasoning = getattr(choice.message, "reasoning_content", None) or getattr(
            choice.message, "reasoning", None
        )
        if reasoning:
            usage["reasoning_preview"] = str(reasoning)[:1200]
        return text, choice.finish_reason, usage, resp

    async def _collect_completion_stream(
        self,
        stream_resp: Any,
        first_token_timeout_s: float,
        first_token_deadline: float | None,
    ) -> tuple[str, str | None, dict[str, int], Any]:
        iterator = stream_resp.__aiter__()
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        chunk_count = 0
        chunk_samples: list[Any] = []
        finish: str | None = None
        usage: dict[str, int] = {}
        provider_usage: dict[str, Any] | None = None
        first_token_seen = False
        inter_token_timeout_s = self._s.llm.timeout_s

        while True:
            try:
                if not first_token_seen and first_token_deadline is not None:
                    remaining = first_token_deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    chunk = await asyncio.wait_for(anext(iterator), timeout=remaining)
                elif first_token_seen and inter_token_timeout_s > 0:
                    # Once a real reasoning/content delta has arrived, the short
                    # first-token health deadline is spent. A subsequent stream stall
                    # uses the configured request timeout instead.
                    chunk = await asyncio.wait_for(anext(iterator), timeout=inter_token_timeout_s)
                else:
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                timeout_phase = "first_token" if not first_token_seen else "inter_token"
                raise LLMTimeoutError(
                    (
                        "provider stream first token timeout"
                        if not first_token_seen
                        else "provider stream inter-token timeout"
                    ),
                    context={
                        "timeout_phase": timeout_phase,
                        "first_token_timeout_s": first_token_timeout_s,
                        "inter_token_timeout_s": inter_token_timeout_s,
                    },
                ) from exc

            chunk_count += 1
            safe_chunk = _json_safe(chunk)
            if chunk_count <= 3:
                chunk_samples.append(safe_chunk)
            elif len(chunk_samples) < 6:
                chunk_samples.append(safe_chunk)
            else:
                chunk_samples[-3:] = chunk_samples[-2:] + [safe_chunk]
            chunk_usage = self._usage_dict(self._get(chunk, "usage"))
            if chunk_usage:
                usage = chunk_usage
            if isinstance(safe_chunk, dict) and isinstance(safe_chunk.get("usage"), dict):
                provider_usage = safe_chunk["usage"]
            for choice in self._get(chunk, "choices", []) or []:
                finish = self._get(choice, "finish_reason") or finish
                delta = self._get(choice, "delta", {}) or {}
                # OpenRouter normalizes the reasoning stream to `delta.reasoning`;
                # native DeepSeek uses `delta.reasoning_content`. Accept both, or long
                # reasoning stretches look token-less to the first-token watchdog.
                reasoning = self._get(delta, "reasoning_content") or self._get(delta, "reasoning")
                content = self._get(delta, "content")
                if reasoning:
                    first_token_seen = True
                    reasoning_parts.append(str(reasoning))
                if content:
                    first_token_seen = True
                    text_parts.append(str(content))

        reasoning_text = "".join(reasoning_parts)
        content_text = "".join(text_parts)
        if reasoning_parts:
            usage["reasoning_preview"] = reasoning_text[:1200]
        return (
            content_text,
            finish,
            usage,
            {
                "stream_chunk_count": chunk_count,
                "stream_chunk_samples": chunk_samples,
                "stream_ended_before_first_token": not first_token_seen,
                "reasoning_char_count": len(reasoning_text),
                "content_char_count": len(content_text),
                "provider_usage": provider_usage,
            },
        )

    def _provider_request_options(
        self,
        *,
        response_format: dict[str, Any] | None,
        reasoning_effort: str | None,
        thinking: str | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if response_format is not None:
            kwargs["response_format"] = response_format
        extra_body: dict[str, Any] = {}
        normalized_effort = (
            str(reasoning_effort).strip().lower() if reasoning_effort is not None else ""
        )
        if normalized_effort == "none" and self._uses_openrouter_endpoint():
            # OpenRouter's explicit disable contract is the reasoning object.  Sending
            # only reasoning_effort="none" is not sufficient evidence that upstream
            # reasoning was disabled (and some endpoints simply ignore that value).
            extra_body["reasoning"] = {"enabled": False}
        elif reasoning_effort:
            kwargs["reasoning_effort"] = str(reasoning_effort)
        if thinking:
            extra_body["thinking"] = {"type": str(thinking)}
        provider_only = self._s.llm.openrouter_provider_only
        if self._budget is not None:
            # Formal campaigns freeze a route-wide ceiling instead of pinning one
            # provider. OpenRouter can therefore reroute capacity failures while the
            # request itself rejects any endpoint priced above the audited snapshot.
            extra_body["provider"] = {
                "allow_fallbacks": True,
                "require_parameters": self._s.llm.openrouter_require_parameters,
                "max_price": {
                    "prompt": self._s.llm.openrouter_max_price_prompt_usd_per_million,
                    "completion": (
                        self._s.llm.openrouter_max_price_completion_usd_per_million
                    ),
                    "request": self._s.llm.openrouter_max_price_request_usd,
                },
            }
        elif provider_only:
            extra_body["provider"] = {
                "only": list(provider_only),
                "allow_fallbacks": self._s.llm.openrouter_allow_fallbacks,
                "require_parameters": self._s.llm.openrouter_require_parameters,
            }
        elif self._uses_openrouter_endpoint():
            extra_body["provider"] = {
                "allow_fallbacks": True,
                "require_parameters": False,
            }
        if extra_body:
            kwargs["extra_body"] = extra_body
        if self._s.llm.openrouter_metadata:
            kwargs["extra_headers"] = {"X-OpenRouter-Metadata": "enabled"}
        return kwargs

    def _uses_openrouter_endpoint(self) -> bool:
        base_url = self._s.llm.base_url.strip()
        parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
        host = (parsed.hostname or "").lower()
        return host == "openrouter.ai" or host.endswith(".openrouter.ai")

    async def _raw_tool_call(
        self,
        agent: str,
        model: str,
        convo: list[Message],
        temperature: float | None,
        max_tokens: int | None,
        tools: list[ToolSchema],
        tool_choice: ToolChoice | None,
        provider_kwargs: dict[str, Any],
        stream: bool,
        first_token_timeout_s: float,
    ) -> tuple[str, str | None, dict[str, int], Any, list[dict[str, Any]]]:
        if self._s.stub:
            return self._stub_tool_call(agent, convo)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": convo,
            "tools": tools,
            "stream": stream,
            **provider_kwargs,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if stream:
            kwargs["stream_options"] = {"include_usage": True}
        # Same contract as _raw_call: the semaphore bounds the WHOLE in-flight call
        # (stream collection included), an abandoned stream is always closed, and a
        # streaming create() must produce response headers within the first-token
        # window (accept-then-stall throttling otherwise hangs until the httpx read
        # timeout).
        async with self._sem or nullcontext():
            async with self._borrow_live_provider() as (provider, shard):
                first_token_deadline = (
                    time.monotonic() + first_token_timeout_s
                    if stream and first_token_timeout_s > 0
                    else None
                )
                try:
                    if first_token_deadline is not None:
                        remaining = first_token_deadline - time.monotonic()
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        resp = await asyncio.wait_for(
                            provider.chat.completions.create(**kwargs),
                            timeout=remaining,
                        )
                    else:
                        resp = await provider.chat.completions.create(**kwargs)
                except asyncio.TimeoutError as exc:
                    self._mark_shard_unhealthy(shard)
                    raise LLMTimeoutError(
                        "provider response headers timeout",
                        context={
                            "timeout_phase": "response_headers",
                            "first_token_timeout_s": first_token_timeout_s,
                        },
                    ) from exc
                except LLMError:
                    raise
                except Exception as exc:  # noqa: BLE001 - mapped to typed anomalies below
                    mapped = self._map_provider_error(exc)
                    if self._stalled_stream_requires_new_transport(mapped):
                        self._mark_shard_unhealthy(shard)
                    raise mapped from exc
                if stream:
                    try:
                        return await self._collect_tool_stream(
                            resp,
                            first_token_timeout_s,
                            first_token_deadline,
                        )
                    except LLMError as err:
                        if self._stalled_stream_requires_new_transport(err):
                            self._mark_shard_unhealthy(shard)
                        raise
                    except Exception as exc:  # noqa: BLE001 - streaming iterator faults are provider faults
                        mapped = self._map_provider_error(exc)
                        if self._stalled_stream_requires_new_transport(mapped):
                            self._mark_shard_unhealthy(shard)
                        raise mapped from exc
                    finally:
                        await self._close_stream(resp)
        choice = resp.choices[0]
        message = choice.message
        text = getattr(message, "content", None) or ""
        usage = self._usage_dict(getattr(resp, "usage", None))
        reasoning = getattr(message, "reasoning_content", None) or getattr(
            message, "reasoning", None
        )
        if reasoning:
            usage["reasoning_content"] = str(reasoning)
            usage["reasoning_preview"] = str(reasoning)[:1200]
        tool_calls = self._normalize_tool_calls(getattr(message, "tool_calls", None))
        return text, choice.finish_reason, usage, resp, tool_calls

    async def _collect_tool_stream(
        self,
        stream_resp: Any,
        first_token_timeout_s: float,
        first_token_deadline: float | None,
    ) -> tuple[str, str | None, dict[str, int], Any, list[dict[str, Any]]]:
        iterator = stream_resp.__aiter__()
        chunks: list[Any] = []
        first_token_seen = False
        inter_token_timeout_s = self._s.llm.timeout_s
        while True:
            try:
                if not first_token_seen and first_token_deadline is not None:
                    remaining = first_token_deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    chunk = await asyncio.wait_for(anext(iterator), timeout=remaining)
                elif first_token_seen and inter_token_timeout_s > 0:
                    chunk = await asyncio.wait_for(anext(iterator), timeout=inter_token_timeout_s)
                else:
                    chunk = await anext(iterator)
            except StopAsyncIteration as exc:
                if not first_token_seen:
                    raise EmptyResponseError("provider stream ended before first token") from exc
                break
            except asyncio.TimeoutError as exc:
                timeout_phase = "first_token" if not first_token_seen else "inter_token"
                raise LLMTimeoutError(
                    (
                        "provider stream first token timeout"
                        if not first_token_seen
                        else "provider stream inter-token timeout"
                    ),
                    context={
                        "timeout_phase": timeout_phase,
                        "first_token_timeout_s": first_token_timeout_s,
                        "inter_token_timeout_s": inter_token_timeout_s,
                    },
                ) from exc
            chunks.append(chunk)
            if self._tool_stream_chunk_has_token(chunk):
                first_token_seen = True
        return self._assemble_tool_stream_chunks(chunks)

    def _tool_stream_chunk_has_token(self, chunk: Any) -> bool:
        """Return whether a chunk carries a real reasoning/content/tool delta."""
        for choice in self._get(chunk, "choices", []) or []:
            delta = self._get(choice, "delta", {}) or {}
            if self._get(delta, "reasoning_content") or self._get(delta, "reasoning"):
                return True
            if self._get(delta, "content"):
                return True
            for call_delta in self._get(delta, "tool_calls", []) or []:
                if self._get(call_delta, "id") or self._get(call_delta, "type"):
                    return True
                function = self._get(call_delta, "function")
                if function is not None and (
                    self._get(function, "name") or self._get(function, "arguments")
                ):
                    return True
        return False

    def _assemble_tool_stream_chunks(
        self,
        chunks: list[Any],
    ) -> tuple[str, str | None, dict[str, int], Any, list[dict[str, Any]]]:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish: str | None = None
        usage: dict[str, int] = {}
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        raw_chunks: list[Any] = []
        for chunk in chunks:
            raw_chunks.append(_json_safe(chunk))
            chunk_usage = self._usage_dict(self._get(chunk, "usage"))
            if chunk_usage:
                usage = chunk_usage
            for choice in self._get(chunk, "choices", []) or []:
                finish = self._get(choice, "finish_reason") or finish
                delta = self._get(choice, "delta", {}) or {}
                # OpenRouter normalizes the reasoning stream to `delta.reasoning`;
                # native DeepSeek uses `delta.reasoning_content`. Accept both, or long
                # reasoning stretches look token-less to the first-token watchdog.
                reasoning = self._get(delta, "reasoning_content") or self._get(delta, "reasoning")
                content = self._get(delta, "content")
                if reasoning:
                    reasoning_parts.append(str(reasoning))
                if content:
                    text_parts.append(str(content))
                for call_delta in self._get(delta, "tool_calls", []) or []:
                    index = self._get(call_delta, "index")
                    if not isinstance(index, int):
                        index = len(tool_calls_by_index)
                    item = tool_calls_by_index.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    call_id = self._get(call_delta, "id")
                    if call_id:
                        item["id"] = str(call_id)
                    call_type = self._get(call_delta, "type")
                    if call_type:
                        item["type"] = str(call_type)
                    function = self._get(call_delta, "function")
                    if function is not None:
                        name = self._get(function, "name")
                        if name:
                            item["function"]["name"] = str(name)
                        arguments = self._get(function, "arguments")
                        if arguments:
                            item["function"]["arguments"] += str(arguments)
        tool_calls = [
            call
            for _index, call in sorted(tool_calls_by_index.items(), key=lambda item: item[0])
            if call.get("id") or call.get("function", {}).get("name")
        ]
        raw = {"stream_chunks": raw_chunks}
        if reasoning_parts:
            reasoning_content = "".join(reasoning_parts)
            usage["reasoning_content"] = reasoning_content
            usage["reasoning_preview"] = reasoning_content[:1200]
        return "".join(text_parts), finish, usage, raw, tool_calls

    def _stub_call(
        self, agent: str, convo: list[Message]
    ) -> tuple[str, str | None, dict[str, int], Any]:
        if self._stub_fn is None:
            payload: str | dict = {"_stub": True, "agent": agent}
        else:
            payload = self._stub_fn(agent, convo, None)
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return text, "stop", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, None

    def _stub_tool_call(
        self,
        agent: str,
        convo: list[Message],
    ) -> tuple[str, str | None, dict[str, int], Any, list[dict[str, Any]]]:
        if self._stub_fn is None:
            payload: str | dict = {"_stub": True, "agent": agent}
        else:
            payload = self._stub_fn(agent, convo, None)
        if isinstance(payload, dict) and "tool_calls" in payload:
            text = str(payload.get("content") or "")
            tool_calls = self._normalize_tool_calls(payload.get("tool_calls"))
            finish = "tool_calls" if tool_calls else "stop"
            return (
                text,
                finish,
                {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                payload,
                tool_calls,
            )
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return (
            text,
            "stop",
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            payload,
            [],
        )

    @staticmethod
    async def _close_stream(stream_resp: Any) -> None:
        """Release an SSE stream (finished OR abandoned).

        Without an explicit close, a timed-out/abandoned stream keeps its pooled HTTP
        connection checked out and the provider keeps GENERATING (and billing) into a
        socket nobody reads — under retry-until-success that compounds into
        self-inflicted provider load. Closing a dead stream must never mask the
        original error, so failures here are swallowed.
        """
        for name in ("aclose", "close"):
            closer = getattr(stream_resp, name, None)
            if not callable(closer):
                continue
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - best-effort release, never masks the real error
                pass
            return

    @staticmethod
    def _get(value: Any, key: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    @classmethod
    def _usage_dict(cls, usage: Any) -> dict[str, int]:
        if not usage:
            return {}
        out = {
            "prompt_tokens": int(cls._get(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(cls._get(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(cls._get(usage, "total_tokens", 0) or 0),
        }
        prompt_details = cls._get(usage, "prompt_tokens_details", {}) or {}
        cached_tokens = int(cls._get(prompt_details, "cached_tokens", 0) or 0)
        out["cache_hit_tokens"] = cached_tokens
        out["cache_miss_tokens"] = max(0, out["prompt_tokens"] - cached_tokens)
        return out

    @classmethod
    def _normalize_tool_calls(cls, tool_calls: Any) -> list[dict[str, Any]]:
        safe = _json_safe(tool_calls)
        if safe is None:
            return []
        if not isinstance(safe, list):
            safe = [safe]
        out: list[dict[str, Any]] = []
        for call in safe:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                function = {}
            arguments = function.get("arguments", "")
            if isinstance(arguments, (dict, list)):
                arguments = json.dumps(arguments, ensure_ascii=False)
            normalized = {
                "id": str(call.get("id") or ""),
                "type": str(call.get("type") or "function"),
                "function": {
                    "name": str(function.get("name") or ""),
                    "arguments": str(arguments or ""),
                },
            }
            if normalized["id"] or normalized["function"]["name"]:
                out.append(normalized)
        return out

    @staticmethod
    def _map_provider_error(exc: Exception) -> LLMError:
        name = type(exc).__name__
        msg = str(exc)[:400]
        raw_status = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if raw_status is None:
            raw_status = getattr(response, "status_code", None)
        if raw_status is None:
            body = getattr(exc, "body", None)
            if isinstance(body, dict):
                raw_status = body.get("code")
                body_error = body.get("error")
                if raw_status is None and isinstance(body_error, dict):
                    raw_status = body_error.get("code")
        try:
            status = int(raw_status) if raw_status is not None else None
        except (TypeError, ValueError):
            status = None
        retry_after_s, retry_after_raw = _retry_after_seconds(exc)
        error_context: dict[str, Any] = {"status_code": status}
        if retry_after_s is not None:
            error_context["retry_after_s"] = retry_after_s
        if retry_after_raw is not None:
            error_context["retry_after_raw"] = retry_after_raw
        if status == 402:
            return CampaignPauseError(
                "OpenRouter balance is insufficient; campaign must pause",
                context={**error_context, "pause_reason": "provider_balance_insufficient"},
            )
        if status == 429 or "RateLimit" in name:
            return RateLimitError(f"provider rate limit: {msg}", context=error_context)
        msg_lower = msg.lower()
        if (
            "Timeout" in name
            or "APIConnection" in name
            or "upstream idle timeout" in msg_lower
            or "gateway timeout" in msg_lower
        ):
            return LLMTimeoutError(
                f"provider timeout/connection: {msg}",
                context=error_context,
                retryable=True,
            )
        # A stream the provider drops mid-body (RemoteProtocolError / incomplete chunked
        # read) is the same transient transport fault as a connect failure: the response
        # never ARRIVED, so the retry-until-arrival policy owns it. Without this, heavy-
        # concurrency stream drops terminally fail k=1 records with pure infra noise.
        if (
            "RemoteProtocol" in name
            or "ChunkedEncoding" in name
            or "IncompleteRead" in name
            # HTTP/2 stream-level faults (server reset/terminated a multiplexed
            # stream): same transient transport class as a dropped HTTP/1.1 body.
            or "EndOfStream" in name
            or "StreamReset" in name
            or "ConnectionTerminated" in name
            or "BrokenResource" in name
            or "LocalProtocol" in name
            or (
                "ProtocolError" in name
                and (
                    "closed" in msg_lower
                    or "recv_data" in msg_lower
                    or "connectionstate" in msg_lower
                )
            )
            # httpx transport read/write faults mid-request: same transient class.
            or "ReadError" in name
            or "WriteError" in name
            or "ConnectError" in name
            or "incomplete chunked read" in msg
            or "peer closed connection" in msg
            or "max outbound streams" in msg_lower
        ):
            return LLMTimeoutError(
                f"provider stream dropped: [{name}] {msg}",
                context={**error_context, "timeout_phase": "transport_closed"},
                retryable=True,
            )
        if "BadRequest" in name and ("context" in msg.lower() or "maximum" in msg.lower()):
            return ContextOverflowError(f"context length exceeded: {msg}", context=error_context)
        # OpenRouter can surface upstream failures as standard/non-standard 5xx statuses.
        # Authentication/authorization and all other permanent 4xx errors fail fast.
        retryable = status in (408, 425) or (status is not None and 500 <= status <= 599)
        return LLMError(
            f"provider error [{name}]: {msg}", context=error_context, retryable=retryable
        )

    @staticmethod
    def _check_response(
        text: str,
        finish: str | None,
        agent: str,
        *,
        provider_metadata: dict[str, Any] | None = None,
    ) -> None:
        refusal = _metadata_refusal(provider_metadata)
        if refusal:
            raise RefusalError(
                "model returned a refusal",
                context={
                    "agent": agent,
                    "finish_reason": finish,
                    "refusal": _safe_repr(refusal, limit=500),
                },
            )
        if finish == "length":
            raise TruncatedResponseError(
                "response truncated (finish_reason=length)",
                context={
                    "agent": agent,
                    "finish_reason": finish,
                    "truncation": (provider_metadata or {}).get("truncation"),
                    "incomplete_details": (provider_metadata or {}).get("incomplete_details"),
                },
            )
        if not text.strip():
            metadata = provider_metadata or {}
            if metadata.get("stream_ended_before_first_token"):
                message = "provider stream ended before first token"
            elif int(metadata.get("reasoning_char_count") or 0) > 0:
                message = "model returned reasoning without answer content"
            else:
                message = "model returned empty content"
            raise EmptyResponseError(
                message,
                context={
                    "agent": agent,
                    "finish_reason": finish,
                    "stream_chunk_count": metadata.get("stream_chunk_count"),
                    "reasoning_char_count": metadata.get("reasoning_char_count"),
                    "content_char_count": metadata.get("content_char_count"),
                },
            )
        low = text.strip().lower()
        if len(low) < 120 and any(low.startswith(m) for m in _REFUSAL_MARKERS):
            raise RefusalError(
                "response looks like a refusal", context={"agent": agent, "preview": text[:200]}
            )

    @staticmethod
    def _check_tool_response(
        text: str,
        tool_calls: list[dict[str, Any]],
        finish: str | None,
        agent: str,
        *,
        provider_metadata: dict[str, Any] | None = None,
    ) -> None:
        refusal = _metadata_refusal(provider_metadata)
        if refusal:
            raise RefusalError(
                "model returned a refusal",
                context={
                    "agent": agent,
                    "finish_reason": finish,
                    "refusal": _safe_repr(refusal, limit=500),
                },
            )
        if finish == "length":
            raise TruncatedResponseError(
                "response truncated (finish_reason=length)",
                context={
                    "agent": agent,
                    "finish_reason": finish,
                    "truncation": (provider_metadata or {}).get("truncation"),
                    "incomplete_details": (provider_metadata or {}).get("incomplete_details"),
                },
            )
        if not text.strip() and not tool_calls:
            raise EmptyResponseError(
                "model returned empty content and no tool calls",
                context={"agent": agent, "finish_reason": finish},
            )

    @staticmethod
    def _should_fallback_tool_choice(err: LLMError) -> bool:
        message = err.message.lower()
        field = str(err.context.get("field") or "").lower()
        return "tool_choice" in message or field == "tool_choice"

    def _tool_choice_disabled_reason(self, model: str) -> str | None:
        if model in self._tool_choice_unsupported_models:
            return "previous provider rejection for this model"
        if self._uses_deepseek_openai_endpoint() and self._deepseek_thinking_enabled(model):
            return _DEEPSEEK_TOOL_CHOICE_DISABLED_REASON
        return None

    def _tool_provider_request_options(self, model: str) -> dict[str, Any]:
        thinking = self._s.llm.thinking
        if (
            thinking is None
            and self._uses_deepseek_openai_endpoint()
            and self._deepseek_thinking_enabled(model)
        ):
            thinking = "enabled"
        return self._provider_request_options(
            response_format=None,
            reasoning_effort=self._s.llm.reasoning_effort,
            thinking=thinking,
        )

    def _uses_deepseek_openai_endpoint(self) -> bool:
        base_url = self._s.llm.base_url.strip()
        parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
        host = parsed.hostname or ""
        path = parsed.path.rstrip("/")
        return host.lower() == _DEEPSEEK_OPENAI_HOST and not path.startswith("/anthropic")

    def _deepseek_thinking_enabled(self, model: str) -> bool:
        thinking = self._s.llm.thinking
        if thinking is not None:
            return str(thinking).strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
                "disable",
                "disabled",
                "none",
            }
        model_name = model.strip().lower().rsplit("/", 1)[-1]
        return model_name != "deepseek-chat"

    @staticmethod
    def _next_provider_attempt_index(attempts: list[dict[str, Any]]) -> int:
        indices: list[int] = []
        for item in attempts:
            if item.get("kind") == _COMPACTED_ATTEMPTS_KIND:
                raw_index = item.get("max_provider_attempt_index")
            else:
                raw_index = item.get("provider_attempt_index")
            try:
                if raw_index is not None:
                    indices.append(int(raw_index))
            except (TypeError, ValueError):
                continue
        return max(indices, default=0) + 1

    @staticmethod
    def _provider_attempt_count(attempts: list[dict[str, Any]]) -> int:
        compacted_count = 0
        compacted_max = 0
        indices: set[int] = set()
        for item in attempts:
            if item.get("kind") == _COMPACTED_ATTEMPTS_KIND:
                compacted_count += int(item.get("provider_attempt_count", 0) or 0)
                compacted_max = max(
                    compacted_max,
                    int(item.get("max_provider_attempt_index", 0) or 0),
                )
                continue
            raw_index = item.get("provider_attempt_index")
            try:
                if raw_index is not None:
                    indices.add(int(raw_index))
            except (TypeError, ValueError):
                continue
        if indices or compacted_count:
            return compacted_count + len({index for index in indices if index > compacted_max})
        # Backward-compatible fallback for diagnostics created before provider
        # attempts carried a stable index.  A send followed by send_error is one
        # provider request, not two.
        sends = sum(1 for item in attempts if item.get("kind") == "send")
        errors_without_response = sum(
            1
            for item in attempts
            if item.get("kind") == "send_error"
            and not (item.get("attempt_diagnostics") or {}).get("response_received")
        )
        return sends + errors_without_response

    @staticmethod
    def _log_run_provider_attempt(
        log: RunLogger,
        *,
        agent: str,
        call_id: str,
        model: str,
        provider_attempt_index: int,
        transport_attempt: int,
        provider_metadata: dict[str, Any] | None,
        response_received: bool,
        latency_s: float,
        usage: dict[str, Any] | None,
        provider_cost_observed: Any,
        error: LLMError | None,
        request_config: dict[str, Any],
        call_status: str | None = None,
        budget_reservation: dict[str, Any] | None = None,
        budget_settlement: dict[str, Any] | None = None,
    ) -> bool:
        """Durably record retries when no TaskLogger campaign ledger is bound."""

        record_cost = getattr(log, "record_llm_cost", None)
        if not callable(record_cost):
            return False
        resolved_latency_s = float(latency_s)
        if not math.isfinite(resolved_latency_s) or resolved_latency_s < 0:
            raise ValueError(
                "provider-attempt latency_s must be finite and non-negative"
            )
        provider_cost = LLMClient._provider_cost_usd(provider_metadata)
        known_rejection_cost = (
            LLMClient._known_rejection_cost(error) if error is not None else None
        )
        if provider_cost is None and known_rejection_cost is not None:
            provider_cost = known_rejection_cost
        campaign_budget = request_config.get("campaign_budget")
        durable = bool(
            isinstance(campaign_budget, dict)
            and campaign_budget.get("durable_cost_ledger")
        )
        resolved_status = call_status or (
            "retry" if error is not None and error.retryable else "error"
        )
        billed_provider_cost = LLMClient._provider_cost_usd(provider_metadata)
        record_cost(
            agent=agent,
            call_id=call_id,
            model=model,
            usage=usage,
            cost_usd=provider_cost,
            cost_source=(
                "known_pre_inference_rejection"
                if known_rejection_cost is not None and billed_provider_cost is None
                else "provider_usage"
                if error is None and provider_cost is not None
                else "provider_usage_retry"
                if provider_cost is not None and resolved_status == "retry"
                else "provider_usage_error"
                if provider_cost is not None
                else "unknown"
            ),
            record_type="provider_attempt",
            provider_attempt_index=provider_attempt_index,
            transport_attempt=transport_attempt,
            call_status=resolved_status,
            status=resolved_status,
            response_received=response_received,
            latency_s=resolved_latency_s,
            anomaly=(error.anomaly.value if error is not None and error.anomaly else None),
            error=error.to_record() if error is not None else None,
            provider_metadata=provider_metadata,
            provider=(provider_metadata or {}).get("provider"),
            openrouter_metadata=(provider_metadata or {}).get("openrouter_metadata"),
            provider_cost_observed=provider_cost_observed,
            settled_cost_usd=(budget_settlement or {}).get("settled_cost_usd"),
            request_config=request_config,
            budget_reservation=budget_reservation,
            budget_settlement=budget_settlement,
            attempt_receipt_id=(budget_settlement or {}).get(
                "attempt_receipt_id"
            ),
            attempt_receipt_sha256=(budget_settlement or {}).get(
                "attempt_receipt_sha256"
            ),
            durable=durable,
        )
        return True

    async def _task_logger_settle_received_attempt(
        self,
        task_logger: "TaskLogger",
        *,
        agent: str,
        call_id: str,
        model: str,
        repair_index: int,
        call_status: str,
        attempts: list[dict[str, Any]],
        request_config: dict[str, Any],
        retry_kind: str | None = None,
        error: LLMError | None = None,
        failure_phase: str | None = None,
    ) -> None:
        """Settle the latest received response after output validation.

        A transport-successful response is not a successful provider attempt
        until any requested JSON/schema validation also accepts it.  Deferring
        this one ledger append lets rejected parse/schema responses carry their
        anomaly evidence on the original provider-attempt row.
        """

        provider_attempt = next(
            (
                item
                for item in reversed(attempts)
                if item.get("kind") == "send"
                and item.get("repair_index") == repair_index
                and not item.get("provider_attempt_ledger_settled")
            ),
            None,
        )
        if provider_attempt is None:
            raise RuntimeError(
                f"no unsettled provider response for call={call_id} repair={repair_index}"
            )
        provider_attempt_index = int(provider_attempt["provider_attempt_index"])
        transport_attempt = int(provider_attempt.get("attempt", 0)) + 1
        self._task_logger_log_attempt(
            task_logger,
            agent=agent,
            call_id=call_id,
            model=model,
            provider_attempt_index=provider_attempt_index,
            transport_attempt=transport_attempt,
            repair_index=repair_index,
            call_status=call_status,
            provider_attempt=provider_attempt,
            request_config=request_config,
            retry_kind=retry_kind,
            error=error,
            failure_phase=failure_phase,
        )
        await self._ack_provider_attempt_receipt(
            provider_attempt.get("budget_settlement")
        )
        provider_attempt["provider_attempt_ledger_settled"] = True

    @staticmethod
    def _task_logger_log_attempt(
        task_logger: "TaskLogger",
        *,
        agent: str,
        call_id: str,
        model: str,
        provider_attempt_index: int,
        transport_attempt: int,
        repair_index: int,
        call_status: str,
        provider_attempt: dict[str, Any] | None,
        request_config: dict[str, Any],
        retry_kind: str | None = None,
        error: LLMError | None = None,
        failure_phase: str | None = None,
        budget_reservation: dict[str, Any] | None = None,
        budget_settlement: dict[str, Any] | None = None,
        latency_s: float | None = None,
    ) -> None:
        """Flush exactly one durable billing/routing row for a provider request."""

        response_received = provider_attempt is not None
        if response_received:
            latency_s = provider_attempt.get("latency_s")
        try:
            resolved_latency_s = float(latency_s) if latency_s is not None else math.nan
        except (TypeError, ValueError):
            resolved_latency_s = math.nan
        if not math.isfinite(resolved_latency_s) or resolved_latency_s < 0:
            raise ValueError(
                "provider-attempt latency_s must be finite and non-negative"
            )
        usage = provider_attempt.get("usage") if response_received else None
        if not isinstance(usage, dict):
            usage = None
        provider_metadata = provider_attempt.get("provider_metadata") if response_received else None
        if not isinstance(provider_metadata, dict):
            provider_metadata = None
        if response_received:
            budget_reservation = budget_reservation or provider_attempt.get(
                "budget_reservation"
            )
            budget_settlement = budget_settlement or provider_attempt.get("budget_settlement")
        response_anomaly_evidence: dict[str, Any] | None = None
        if response_received and call_status != "success":
            existing_evidence = provider_attempt.get("response_anomaly_evidence")
            if isinstance(existing_evidence, dict):
                response_anomaly_evidence = existing_evidence
            else:
                response_text = str(provider_attempt.get("response") or "")
                try:
                    response_anomaly_evidence = task_logger.write_llm_response_anomaly_evidence(
                        call_id,
                        provider_attempt_index=provider_attempt_index,
                        transport_attempt=transport_attempt,
                        repair_index=repair_index,
                        response_text=response_text,
                        call_status=call_status,
                        failure_phase=failure_phase or "provider_response_validation",
                        anomaly=(
                            error.anomaly.value if error is not None and error.anomaly else None
                        ),
                        error_type=(type(error).__name__ if error is not None else None),
                        finish_reason=provider_attempt.get("finish_reason"),
                    )
                    response_anomaly_evidence["write_status"] = "written"
                except Exception as evidence_error:  # noqa: BLE001 - preserve billing row
                    response_anomaly_evidence = {
                        "schema": "tend.provider_response_anomaly.v1",
                        "write_status": "error",
                        "error_type": type(evidence_error).__name__,
                        "error_message": str(evidence_error),
                    }
                    task_logger.warning(
                        "llm_response_anomaly_evidence_write_failed",
                        call_id=call_id,
                        provider_attempt_index=provider_attempt_index,
                        repair_index=repair_index,
                        error_type=type(evidence_error).__name__,
                        error_message=str(evidence_error),
                    )
                provider_attempt["response_anomaly_evidence"] = response_anomaly_evidence
        provider_cost = LLMClient._provider_cost_usd(provider_metadata)
        known_rejection_cost = (
            LLMClient._known_rejection_cost(error) if error is not None else None
        )
        if provider_cost is None and known_rejection_cost is not None:
            provider_cost = known_rejection_cost
        if known_rejection_cost is not None and LLMClient._provider_cost_usd(
            provider_metadata
        ) is None:
            cost_source = "known_pre_inference_rejection"
        elif provider_cost is None:
            cost_source = "unknown"
        elif call_status == "retry":
            cost_source = "provider_usage_retry"
        elif call_status == "error":
            cost_source = "provider_usage_error"
        else:
            cost_source = "provider_usage"
        task_logger.log_llm_attempt(
            call_id,
            agent=agent,
            model=model,
            provider_attempt_index=provider_attempt_index,
            transport_attempt=transport_attempt,
            repair_index=repair_index,
            call_status=call_status,
            response_received=response_received,
            latency_s=resolved_latency_s,
            usage=usage,
            finish_reason=(provider_attempt.get("finish_reason") if response_received else None),
            cost_usd=provider_cost,
            cost_source=cost_source,
            provider_cost_observed=(
                provider_attempt.get("provider_cost_observed")
                if response_received
                else None
            ),
            provider_metadata=provider_metadata,
            request_config=request_config,
            retry_kind=retry_kind,
            anomaly=(error.anomaly.value if error is not None and error.anomaly else None),
            error=error.to_record() if error is not None else None,
            response_anomaly_evidence=response_anomaly_evidence,
            budget_reservation=budget_reservation,
            budget_settlement=budget_settlement,
            attempt_receipt_id=(budget_settlement or {}).get(
                "attempt_receipt_id"
            ),
            attempt_receipt_sha256=(budget_settlement or {}).get(
                "attempt_receipt_sha256"
            ),
            durable=bool(
                isinstance(request_config.get("campaign_budget"), dict)
                and request_config["campaign_budget"].get("durable_cost_ledger")
            ),
        )

    @staticmethod
    def _task_logger_log_error(
        task_logger: "TaskLogger",
        call_id: str,
        model: str,
        *,
        attempts: list[dict[str, Any]],
        request_config: dict[str, Any],
        error: BaseException,
    ) -> str:
        """Close out a failed TaskLogger-logged call, returning its ``llm/<call_id>.md`` ref."""
        provider_attempt = next(
            (item for item in reversed(attempts) if item.get("kind") == "send"),
            {},
        )
        usage = provider_attempt.get("usage")
        if not isinstance(usage, dict):
            usage = {"prompt_tokens": 0, "completion_tokens": 0}
        provider_metadata = provider_attempt.get("provider_metadata")
        provider_cost = LLMClient._provider_cost_usd(provider_metadata)
        task_logger.log_llm_response(
            call_id,
            response_raw={
                "model": model,
                "call_status": "error",
                "attempt_count": LLMClient._provider_attempt_count(attempts),
                "request_config": request_config,
                "provider_metadata": provider_metadata,
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
            },
            usage=usage,
            finish_reason="error",
            cost_usd=provider_cost or 0.0,
            cost_source="provider_usage_error" if provider_cost is not None else "error",
            append_cost_record=False,
        )
        return str(getattr(task_logger, "last_llm_call_path", None) or "")

    @staticmethod
    def _cost_source(usage: dict[str, int]) -> str:
        token_keys = ("prompt_tokens", "completion_tokens", "total_tokens")
        if any(int(usage.get(key, 0) or 0) > 0 for key in token_keys):
            return "token_usage_only"
        return "unavailable"

    @staticmethod
    def _provider_cost_usd(provider_metadata: Any) -> float | None:
        """Return only a finite, non-negative provider-reported cost for logging."""

        raw_cost = LLMClient._raw_provider_cost(provider_metadata)
        if raw_cost is None:
            return None
        try:
            cost = float(raw_cost)
        except (TypeError, ValueError):
            return None
        return cost if math.isfinite(cost) and cost >= 0 else None

    @staticmethod
    def _raw_provider_cost(provider_metadata: Any) -> Any:
        """Return the receipt's raw cost so the budget ledger can reject invalid values."""

        if not isinstance(provider_metadata, dict):
            return None
        provider_usage = provider_metadata.get("provider_usage")
        if not isinstance(provider_usage, dict):
            return None
        return provider_usage.get("cost")

    @staticmethod
    def _reasoning_content(usage: dict[str, Any]) -> str:
        reasoning = usage.get("reasoning_content") or usage.get("reasoning_preview")
        return str(reasoning) if reasoning else ""

    def _notify_usage_progress(
        self,
        *,
        call_id: str,
        usage: dict[str, int],
        cost_source: str,
    ) -> None:
        callback = self.on_usage
        if callback is None:
            return
        try:
            callback(
                None,
                int(usage.get("prompt_tokens", 0) or 0),
                int(usage.get("completion_tokens", 0) or 0),
                cost_source,
                call_id,
                0,
                0,
            )
        except Exception as exc:
            self._log_progress_callback_failure(
                "on_usage",
                exc,
                call_id=call_id,
            )

    def _retries_exhausted(self, attempts_done: int) -> bool:
        """Provider-fault retry budget. ``max_retries < 0`` retries forever."""
        max_retries = self._s.llm.max_retries
        return max_retries >= 0 and attempts_done >= max_retries

    def _provider_retry_delay(self, retry_number: int, err: LLMError) -> float:
        """Use legacy fixed waits normally and full jitter only in formal campaigns."""

        cfg = self._s.llm
        if self._budget is None:
            return max(0.0, cfg.retry_interval_s)
        number = max(1, int(retry_number))
        if number == 1:
            low = max(0.0, cfg.retry_jitter_initial_min_s)
            high = max(low, cfg.retry_jitter_initial_max_s)
        else:
            low = 0.0
            cap = max(0.0, cfg.retry_jitter_cap_s)
            initial = max(0.0, cfg.retry_jitter_initial_max_s)
            if initial <= 0 or cap <= 0:
                high = 0.0
            elif initial >= cap:
                high = cap
            else:
                steps_to_cap = math.ceil(math.log2(cap / initial))
                high = (
                    cap
                    if number - 1 >= steps_to_cap
                    else initial * (2 ** (number - 1))
                )
        jitter = random.uniform(low, high) if high > low else high
        try:
            parsed_retry_after = float(err.context.get("retry_after_s") or 0.0)
            retry_after = (
                max(0.0, parsed_retry_after) if math.isfinite(parsed_retry_after) else 0.0
            )
        except (TypeError, ValueError):
            retry_after = 0.0
        return max(jitter, retry_after)

    def _budget_request_config(self) -> dict[str, Any]:
        cfg = self._s.llm
        if self._budget is None:
            return {"enabled": False}
        ledger = cfg.openrouter_budget_ledger
        return {
            "enabled": True,
            "cap_usd": cfg.openrouter_campaign_budget_usd,
            "request_reservation_usd": cfg.openrouter_request_reservation_usd,
            "durable_cost_ledger": cfg.openrouter_durable_cost_ledger,
            "pricing_profile_sha256": cfg.openrouter_pricing_profile_sha256,
            "provider_max_price": {
                "prompt_usd_per_million": (
                    cfg.openrouter_max_price_prompt_usd_per_million
                ),
                "completion_usd_per_million": (
                    cfg.openrouter_max_price_completion_usd_per_million
                ),
                "request_usd": cfg.openrouter_max_price_request_usd,
            },
            "ledger_path_sha256": hashlib.sha256(ledger.encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def _budget_snapshot_view(state: dict[str, Any] | None) -> dict[str, Any] | None:
        if state is None:
            return None
        active = state.get("active_reservations")
        active_count = len(active) if isinstance(active, dict) else 0
        cap = float(state.get("cap_usd", 0.0) or 0.0)
        known = float(state.get("known_cost_usd", 0.0) or 0.0)
        unknown = float(state.get("unknown_spend_usd", 0.0) or 0.0)
        reserved = float(state.get("reserved_usd", 0.0) or 0.0)
        unaccounted = float(state.get("unaccounted_provider_cost_usd", 0.0) or 0.0)
        accounted_remaining = cap - known - unknown - reserved
        effective_remaining = accounted_remaining - unaccounted
        return {
            "schema": state.get("schema"),
            "cap_usd": cap,
            "known_cost_usd": known,
            "unknown_spend_usd": unknown,
            "reserved_usd": reserved,
            "remaining_usd": effective_remaining,
            "accounted_remaining_usd": accounted_remaining,
            "effective_remaining_usd": effective_remaining,
            "unaccounted_provider_cost_usd": unaccounted,
            "paused": bool(state.get("paused", False)),
            "pause_reason": state.get("pause_reason"),
            "active_reservation_count": active_count,
            "settled_attempts": int(state.get("settled_attempts", 0) or 0),
            "settlement_pause_triggered": bool(
                state.get("settlement_pause_triggered", False)
            ),
            "settlement_preexisting_pause_reason": state.get(
                "settlement_preexisting_pause_reason"
            ),
            "settlement_pause_reason": state.get("settlement_pause_reason"),
            "attempt_receipt_id": state.get("attempt_receipt_id"),
            "attempt_receipt_sha256": state.get("attempt_receipt_sha256"),
            "started_attempt_receipt_sha256": state.get(
                "started_attempt_receipt_sha256"
            ),
        }

    def _attempt_receipt_template(
        self,
        *,
        agent: str,
        call_id: str,
        model: str,
        provider_attempt_index: int,
        transport_attempt: int,
        repair_index: int,
        status: str,
        request_config: dict[str, Any],
        task_logger: "TaskLogger | None" = None,
        log: RunLogger | None = None,
        provider_attempt: dict[str, Any] | None = None,
        error: LLMError | None = None,
        latency_s: float = 0.0,
        budget_reservation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        manager = getattr(task_logger, "_manager", None)
        if manager is None:
            manager = getattr(log, "manager", None)
        formal_context = getattr(manager, "formal_attempt_receipt_context", None)
        if not isinstance(formal_context, dict):
            formal_context = {}
        task_id = str(
            getattr(task_logger, "task_id", None)
            or formal_context.get("task_id")
            or "unknown"
        )
        stage = str(
            getattr(task_logger, "stage", None)
            or formal_context.get("stage")
            or "unknown"
        )
        provider_metadata = (
            provider_attempt.get("provider_metadata")
            if isinstance(provider_attempt, dict)
            else None
        )
        usage = (
            provider_attempt.get("usage")
            if isinstance(provider_attempt, dict)
            else None
        )
        response_received = provider_attempt is not None
        provider_cost = self._provider_cost_usd(provider_metadata)
        billed_provider_cost = provider_cost
        known_rejection_cost = (
            self._known_rejection_cost(error) if error is not None else None
        )
        if provider_cost is None and known_rejection_cost is not None:
            provider_cost = known_rejection_cost
        return {
            "receipt_context": {
                "campaign_binding": (
                    self._budget.pricing_profile_sha256
                    if self._budget is not None
                    else None
                ),
                "formal_campaign_manifest_sha256": formal_context.get(
                    "formal_campaign_manifest_sha256"
                ),
                "formal_arm_id": formal_context.get("formal_arm_id"),
                "formal_db_id": formal_context.get("formal_db_id"),
                "task_id": task_id,
            },
            "call_id": call_id,
            "agent": agent,
            "model": model,
            "provider_attempt_index": provider_attempt_index,
            "transport_attempt": transport_attempt,
            "repair_index": repair_index,
            "stage": stage,
            "task_id": task_id,
            "status": status,
            "response_received": response_received,
            "latency_s": latency_s,
            "usage": usage,
            "finish_reason": (
                provider_attempt.get("finish_reason")
                if isinstance(provider_attempt, dict)
                else None
            ),
            "provider_metadata": provider_metadata,
            "provider_cost_observed": (
                provider_attempt.get("provider_cost_observed")
                if isinstance(provider_attempt, dict)
                else None
            ),
            "cost_usd": provider_cost,
            "cost_source": (
                "known_pre_inference_rejection"
                if known_rejection_cost is not None and billed_provider_cost is None
                else "provider_usage"
                if provider_cost is not None
                else "unknown"
            ),
            "anomaly": (
                error.anomaly.value if error is not None and error.anomaly else None
            ),
            "error": error.to_record() if error is not None else None,
            "request_config": request_config,
            "budget_reservation": budget_reservation,
        }

    async def _run_ledger_io(
        self,
        operation: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        executor = self._ledger_executor
        if executor is None:
            raise RuntimeError("campaign ledger executor is not available")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, partial(operation, *args, **kwargs))

    async def _ensure_ledger_initialized(self) -> None:
        if self._budget is None or self._ledger_initialized:
            return
        async with self._ledger_init_lock:
            if self._ledger_initialized:
                return
            await self._run_ledger_io(self._budget.initialize)
            self._ledger_initialized = True

    async def _reserve_provider_budget(
        self,
        call_id: str,
        provider_attempt_index: int,
        *,
        attempt_receipt: dict[str, Any] | None = None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        if self._budget is None:
            return None, None
        await self._ensure_ledger_initialized()
        reservation_id = (
            f"{call_id}:{provider_attempt_index}:{os.getpid()}:{uuid4().hex[:10]}"
        )
        contention_round = 0
        while True:
            admission_acquired = False
            try:
                if self._budget_admission_sem is not None:
                    await self._budget_admission_sem.acquire()
                    admission_acquired = True
                state = await self._run_ledger_io(
                    self._budget.reserve,
                    reservation_id,
                    self._s.llm.openrouter_request_reservation_usd,
                    attempt_receipt=attempt_receipt,
                )
                break
            except BudgetReservationBusy:
                if admission_acquired and self._budget_admission_sem is not None:
                    self._budget_admission_sem.release()
                contention_round += 1
                # This is local budget admission, not a provider fault.  Wait without
                # holding the LLM semaphore and retry the same logical provider index.
                high = min(0.25, 0.025 * (2 ** min(contention_round, 4)))
                await asyncio.sleep(random.uniform(0.025, high))
            except BaseException:
                if admission_acquired and self._budget_admission_sem is not None:
                    self._budget_admission_sem.release()
                raise
        view = self._budget_snapshot_view(state)
        if view is not None:
            view["reservation_id"] = reservation_id
            view["reservation_usd"] = self._s.llm.openrouter_request_reservation_usd
        return reservation_id, view

    async def _settle_provider_budget(
        self,
        reservation_id: str | None,
        *,
        known_cost_usd: Any,
        call_id: str,
        provider_attempt_index: int,
        settlement_kind: str,
        pause_reason: str | None = None,
        raise_on_pause: bool = True,
        attempt_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if self._budget is None or reservation_id is None:
            return None
        await self._ensure_ledger_initialized()
        try:
            state = await self._run_ledger_io(
                self._budget.settle,
                reservation_id,
                known_cost_usd=known_cost_usd,
                call_id=call_id,
                provider_attempt_index=provider_attempt_index,
                settlement_kind=settlement_kind,
                pause_reason=pause_reason,
                raise_on_pause=raise_on_pause,
                attempt_receipt=attempt_receipt,
            )
        finally:
            if self._budget_admission_sem is not None:
                self._budget_admission_sem.release()
        view = self._budget_snapshot_view(state)
        if view is not None:
            view["reservation_id"] = reservation_id
            view["settled_cost_usd"] = self._provider_cost_from_raw(known_cost_usd)
            view["settlement_kind"] = "known" if known_cost_usd is not None else "unknown"
        return view

    async def _ack_provider_attempt_receipt(
        self,
        budget_settlement: dict[str, Any] | None,
    ) -> None:
        if self._budget is None or not isinstance(budget_settlement, dict):
            return
        await self._ensure_ledger_initialized()
        receipt_id = budget_settlement.get("attempt_receipt_id")
        receipt_hash = budget_settlement.get("attempt_receipt_sha256")
        if receipt_id is None and receipt_hash is None:
            return
        if not isinstance(receipt_id, str) or not isinstance(receipt_hash, str):
            raise CampaignPauseError(
                "provider attempt settlement lacks its WAL receipt identity",
                context={"pause_reason": "attempt_receipt_identity_missing"},
            )
        await self._run_ledger_io(
            self._budget.acknowledge_attempt_receipt,
            receipt_id,
            receipt_hash,
        )

    @staticmethod
    def _provider_cost_from_raw(value: Any) -> float | None:
        try:
            cost = float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        return cost if cost is not None and math.isfinite(cost) and cost >= 0 else None

    def _settled_provider_error_cost(
        self, err: LLMError, provider_attempt: dict[str, Any] | None
    ) -> float | None:
        """Prefer a billed completion cost; otherwise a known-zero transport rejection."""
        metadata = (
            provider_attempt.get("provider_metadata")
            if isinstance(provider_attempt, dict)
            else None
        )
        billed = self._provider_cost_usd(metadata)
        if billed is not None:
            return billed
        return self._known_rejection_cost(err)

    @staticmethod
    def _known_rejection_cost(err: LLMError) -> float | None:
        """Return zero when there is no evidence the provider billed a completion.

        OpenRouter 401/402/403/429 rejections are known pre-inference responses.
        Connection failures, first-token timeouts, and retryable 5xx/408 statuses
        also settle at zero: the campaign retries them onto another route, and
        charging the full worst-case reservation as unknown on each miss exhausts
        a $60 ledger before any answer can arrive. Incomplete responses that
        carried content but no numeric cost still return None so they stay unknown.
        Content-bearing retryable faults (truncated/empty/parse) also return None
        so settlement can use the billed usage instead of pretending the call was free.
        """

        if isinstance(
            err,
            (
                TruncatedResponseError,
                EmptyResponseError,
                PromptAnomalyError,
                ResponseParseError,
                SchemaValidationError,
                RefusalError,
                ContextOverflowError,
            ),
        ):
            return None
        try:
            status = int(err.context.get("status_code"))
        except (TypeError, ValueError):
            status = None
        if status in {401, 402, 403, 408, 425, 429}:
            return 0.0
        if status is not None and 500 <= status <= 599:
            return 0.0
        if isinstance(err, (LLMTimeoutError, RateLimitError)):
            return 0.0
        if getattr(err, "retryable", False):
            return 0.0
        return None

    def _formal_pause_reason(self, err: LLMError) -> str | None:
        """Classify errors that must stop every worker in a formal campaign."""

        if self._budget is None:
            return None
        if isinstance(err, CampaignPauseError):
            return str(err.context.get("pause_reason") or "campaign_pause_error")
        try:
            status = int(err.context.get("status_code"))
        except (TypeError, ValueError):
            status = None
        if status == 402:
            return "provider_balance_insufficient"
        if status in {401, 403}:
            return "provider_authentication_or_authorization_failed"
        if isinstance(err, ContextOverflowError):
            return "context_overflow"
        if isinstance(err, PromptAnomalyError):
            return "prompt_malformed"
        if err.anomaly == Anomaly.INTERNAL:
            return "llm_client_internal_error"
        return None

    @staticmethod
    def _campaign_pause_from_error(err: LLMError, reason: str) -> CampaignPauseError:
        if isinstance(err, CampaignPauseError):
            return err
        return CampaignPauseError(
            "formal OpenRouter campaign paused after a permanent failure",
            context={
                "pause_reason": reason,
                "source_error": err.to_record(),
            },
        )

    async def _pause_formal_campaign_error(self, err: LLMError) -> LLMError:
        """Persist a pre-send/internal pause and return the supervisor-facing error."""

        reason = self._formal_pause_reason(err)
        if reason is None or self._budget is None:
            return err
        if not isinstance(err, CampaignPauseError):
            await self._ensure_ledger_initialized()
            state = await self._run_ledger_io(
                self._budget.pause,
                reason,
                details={"source_error": err.to_record()},
            )
            reason = str(state.get("pause_reason") or reason)
        return self._campaign_pause_from_error(err, reason)

    def _compact_attempts(self, attempts: list[dict[str, Any]]) -> None:
        """Bound TaskLogger-path RAM after each attempt is durably appended.

        The compact summary preserves exact logical-call counts and the next contiguous
        provider index.  The campaign ledger in ``cost_summary.jsonl`` remains the full
        per-attempt source of truth; only the in-memory diagnostic window is truncated.
        """

        limit = max(1, int(self._s.llm.attempt_memory_limit))
        summary = next(
            (item for item in attempts if item.get("kind") == _COMPACTED_ATTEMPTS_KIND),
            None,
        )
        entries = [item for item in attempts if item.get("kind") != _COMPACTED_ATTEMPTS_KIND]
        indices = sorted(
            {
                int(item["provider_attempt_index"])
                for item in entries
                if item.get("provider_attempt_index") is not None
            }
        )
        if len(indices) <= limit:
            return
        drop_indices = set(indices[:-limit])
        kept = [
            item
            for item in entries
            if item.get("provider_attempt_index") is None
            or int(item["provider_attempt_index"]) not in drop_indices
        ]
        old_count = int((summary or {}).get("provider_attempt_count", 0) or 0)
        old_dropped = int((summary or {}).get("dropped_entries", 0) or 0)
        old_max = int((summary or {}).get("max_provider_attempt_index", 0) or 0)
        new_summary = {
            "kind": _COMPACTED_ATTEMPTS_KIND,
            "provider_attempt_count": old_count + len(drop_indices),
            "max_provider_attempt_index": max(old_max, max(drop_indices, default=old_max)),
            "dropped_entries": old_dropped + len(entries) - len(kept),
            "durable_ledger": "cost_summary.jsonl",
        }
        attempts[:] = [new_summary, *kept]

    def _notify_retry_progress(
        self,
        err: LLMError,
        attempt: int,
        wait_s: float,
    ) -> None:
        callback = self.on_retry
        if callback is None:
            return
        if isinstance(err, RateLimitError):
            reason = "rate_limit"
        elif isinstance(err, EmptyResponseError):
            reason = "empty_content"
        elif isinstance(err, ResponseParseError):
            reason = "structured_parse"
        elif isinstance(err, LLMTimeoutError):
            reason = "network_transient"
        else:
            reason = "api_transient"
        max_attempts = None if self._s.llm.max_retries < 0 else self._s.llm.max_retries + 1
        try:
            callback(
                reason,
                attempt,
                wait_s,
                type(err).__name__,
                max_attempts,
            )
        except Exception as exc:
            self._log_progress_callback_failure(
                "on_retry",
                exc,
                attempt=attempt,
                reason=reason,
            )

    def _log_progress_callback_failure(
        self,
        callback_name: str,
        exc: Exception,
        **fields: Any,
    ) -> None:
        key = f"{callback_name}:{type(exc).__name__}"
        if key in self._progress_callback_failures_seen:
            return
        self._progress_callback_failures_seen.add(key)
        try:
            self._log.warning(
                "llm_progress_callback_failed",
                callback=callback_name,
                error_type=type(exc).__name__,
                message=str(exc),
                **fields,
            )
        except Exception:
            pass

    @staticmethod
    def _repair_prompt(err: LLMError, schema: dict | None) -> str:
        detail = err.context.get("violations") or [err.message]
        lines = "\n".join(f"  - {d}" for d in detail)
        tail = ""
        if schema is not None and err.anomaly == Anomaly.SCHEMA_INVALID:
            tail = (
                "\nReturn ONLY a JSON object that conforms to the required schema. "
                "Do not include prose or code fences."
            )
        return f"Your previous reply was rejected:\n{lines}\nFix it and reply again.{tail}"

    def _finish(
        self,
        agent: str,
        call_id: str,
        model: str,
        text: str,
        parsed: Any,
        finish: str | None,
        usage: dict[str, int],
        t0: float,
        attempts: list[dict[str, Any]],
        log: RunLogger,
        *,
        messages: list[Message],
        request_config: dict[str, Any],
        task_logger: "TaskLogger | None" = None,
    ) -> LLMResult:
        latency = round(time.monotonic() - t0, 3)
        provider_metadata = next(
            (
                item.get("provider_metadata")
                for item in reversed(attempts)
                if item.get("provider_metadata")
            ),
            None,
        )
        if task_logger is not None:
            provider_cost = self._provider_cost_usd(provider_metadata)
            cost_source = (
                "provider_usage" if provider_cost is not None else self._cost_source(usage)
            )
            message: dict[str, Any] = {"role": "assistant", "content": text}
            reasoning_content = self._reasoning_content(usage)
            if reasoning_content:
                message["reasoning_content"] = reasoning_content
            task_logger.log_llm_response(
                call_id,
                response_raw={
                    "model": model,
                    "call_status": "success",
                    "attempt_count": self._provider_attempt_count(attempts),
                    "choices": [{"message": message, "finish_reason": finish}],
                    "usage": usage,
                    "provider_metadata": provider_metadata,
                    "request_config": request_config,
                },
                usage=usage,
                finish_reason=finish or "unknown",
                cost_usd=provider_cost or 0.0,
                cost_source=cost_source,
                append_cost_record=False,
            )
            self._notify_usage_progress(
                call_id=call_id,
                usage=usage,
                cost_source=cost_source,
            )
            ref = str(getattr(task_logger, "last_llm_call_path", None) or "")
            return LLMResult(
                agent=agent,
                call_id=call_id,
                model=model,
                text=text,
                parsed=parsed,
                finish_reason=finish,
                usage=usage,
                latency_s=latency,
                attempts=self._provider_attempt_count(attempts),
                transcript_ref=ref,
                diagnostics_ref=ref,
            )
        ref = log.save_transcript(
            agent,
            call_id,
            {
                "model": model,
                **request_config,
                "messages": messages,
                "attempts": attempts,
                "response_text": text,
                "parsed": parsed,
                "finish_reason": finish,
                "usage": usage,
                "latency_s": latency,
                "parsed_ok": parsed is not None,
                "provider_metadata": provider_metadata,
            },
        )
        diagnostics_ref = _llm_diagnostics_ref(log, agent, call_id, transcript_ref=ref)
        if self._s.llm.slow_call_warn_s > 0 and latency >= self._s.llm.slow_call_warn_s:
            log.warning(
                "llm_slow_call",
                agent=agent,
                call_id=call_id,
                model=model,
                latency_s=latency,
                threshold_s=self._s.llm.slow_call_warn_s,
                attempts=len(attempts),
                transcript_ref=ref,
                diagnostics_ref=diagnostics_ref,
            )
        log.info(
            "llm_call_ok",
            agent=agent,
            call_id=call_id,
            model=model,
            attempts=len(attempts),
            latency_s=latency,
            total_tokens=usage.get("total_tokens", 0),
            transcript_ref=ref,
            diagnostics_ref=diagnostics_ref,
        )
        self._notify_usage_progress(
            call_id=call_id,
            usage=usage,
            cost_source=self._cost_source(usage),
        )
        return LLMResult(
            agent=agent,
            call_id=call_id,
            model=model,
            text=text,
            parsed=parsed,
            finish_reason=finish,
            usage=usage,
            latency_s=latency,
            attempts=self._provider_attempt_count(attempts),
            transcript_ref=ref,
            diagnostics_ref=diagnostics_ref,
        )

    def _finish_tools(
        self,
        agent: str,
        call_id: str,
        model: str,
        text: str,
        finish: str | None,
        usage: dict[str, int],
        raw: Any,
        tool_calls: list[dict[str, Any]],
        t0: float,
        attempts: list[dict[str, Any]],
        log: RunLogger,
        *,
        messages: list[Message],
        request_config: dict[str, Any],
        tool_choice_fallback: bool,
    ) -> ToolLLMResult:
        latency = round(time.monotonic() - t0, 3)
        provider_metadata = next(
            (
                item.get("provider_metadata")
                for item in reversed(attempts)
                if item.get("provider_metadata")
            ),
            None,
        )
        cost_source = self._cost_source(usage)
        assistant_message: Message = {"role": "assistant", "content": text}
        reasoning_content = self._reasoning_content(usage)
        if reasoning_content:
            assistant_message["reasoning_content"] = reasoning_content
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        ref = log.save_transcript(
            agent,
            call_id,
            {
                "model": model,
                **request_config,
                "messages": messages,
                "tool_choice_fallback": tool_choice_fallback,
                "attempts": attempts,
                "assistant_message": assistant_message,
                "response_text": text,
                "tool_calls": tool_calls,
                "finish_reason": finish,
                "usage": usage,
                "latency_s": latency,
                "parsed_ok": None,
                "provider_metadata": provider_metadata,
                "raw_response": _json_safe(raw),
                "cost_source": cost_source,
            },
        )
        diagnostics_ref = _llm_diagnostics_ref(log, agent, call_id, transcript_ref=ref)
        if self._s.llm.slow_call_warn_s > 0 and latency >= self._s.llm.slow_call_warn_s:
            log.warning(
                "llm_slow_call",
                agent=agent,
                call_id=call_id,
                model=model,
                latency_s=latency,
                threshold_s=self._s.llm.slow_call_warn_s,
                attempts=len(attempts),
                transcript_ref=ref,
                diagnostics_ref=diagnostics_ref,
            )
        log.info(
            "llm_call_ok",
            agent=agent,
            call_id=call_id,
            model=model,
            attempts=len(attempts),
            latency_s=latency,
            total_tokens=usage.get("total_tokens", 0),
            tool_calls=len(tool_calls),
            cost_source=cost_source,
            transcript_ref=ref,
            diagnostics_ref=diagnostics_ref,
        )
        self._notify_usage_progress(
            call_id=call_id,
            usage=usage,
            cost_source=cost_source,
        )
        parsed_tool_calls = parse_tool_calls(tool_calls)
        cost = {
            "source": cost_source,
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
        return ToolLLMResult(
            agent=agent,
            call_id=call_id,
            model=model,
            assistant_message=assistant_message,
            tool_calls=parsed_tool_calls,
            cost=cost,
            text=text,
            finish_reason=finish,
            usage=usage,
            latency_s=latency,
            attempts=len(attempts),
            transcript_ref=ref,
            diagnostics_ref=diagnostics_ref,
            provider_metadata=provider_metadata or {},
            tool_choice_fallback=tool_choice_fallback,
        )
