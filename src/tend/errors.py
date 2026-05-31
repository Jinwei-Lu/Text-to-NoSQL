"""Exception taxonomy — the backbone of TEND's anomaly capture.

Every failure mode the pipeline can hit is a typed exception carrying a structured
``context`` dict and an ``anomaly`` classification. The logging layer (observability/
logging.py) serializes these verbatim into ``anomalies.jsonl`` so that an operator —
human or Claude Code — can ``grep`` one stream and see *exactly* what broke, on which
agent/db/record, and (for LLM faults) the offending prompt fingerprint.

Rule of thumb:
  - Raise the *most specific* subclass; never raise bare ``Exception`` in pipeline code.
  - Attach context (db_id, record_id, agent, prompt_ref, ...) at raise sites.
  - LLM-facing faults set ``anomaly`` so they surface in the anomaly stream and the UI.
"""
from __future__ import annotations

import enum
import traceback
from typing import Any


class Anomaly(str, enum.Enum):
    """Coarse classification used for filtering/alerting.

    Values are stable strings (used as log keys and CLI filters); do not rename
    without updating dashboards/greps.
    """

    # --- LLM transport / protocol ---
    API_ERROR = "api_error"                # provider returned 4xx/5xx (non-rate-limit)
    RATE_LIMIT = "rate_limit"              # 429 / quota
    TIMEOUT = "timeout"                    # request exceeded deadline
    EMPTY_RESPONSE = "empty_response"      # model returned no content
    TRUNCATED = "truncated"                # finish_reason=length (cut off mid-answer)
    REFUSAL = "refusal"                    # model refused / safety stop
    # --- LLM output well-formedness ("prompt anomalies") ---
    PROMPT_MALFORMED = "prompt_malformed"  # we built an invalid prompt (pre-send)
    CONTEXT_OVERFLOW = "context_overflow"  # prompt too large for the model window
    PARSE_ERROR = "parse_error"            # response not parseable as expected (JSON)
    SCHEMA_INVALID = "schema_invalid"      # parsed output violated the I/O JSON Schema
    CONTRACT_VIOLATION = "contract_violation"  # output broke an agent semantic contract
    # --- deterministic stages ---
    EXEC_ERROR = "exec_error"              # MongoDB / mongosh execution failed
    DISABLED_OPERATOR = "disabled_operator"  # one of the 6 banned operators appeared
    GOLD_LOCK_FAILED = "gold_lock_failed"  # gold !=_rec reference oracle / dual-path
    GATE_FAILED = "gate_failed"            # a publish gate rejected the candidate
    MIGRATION_ERROR = "migration_error"    # DM produced inconsistent witness data
    SUPPLY_EXHAUSTED = "supply_exhausted"  # coverage cell infeasible / no candidates
    INTERNAL = "internal"                  # unexpected bug (wraps stray exceptions)


class TendError(Exception):
    """Base class for every pipeline error.

    Parameters
    ----------
    message:
        Human-readable summary (shown in the UI and the message field).
    anomaly:
        Optional classification; when set, the error is routed to the anomaly stream.
    context:
        Arbitrary structured fields (db_id, record_id, agent, stage, prompt_ref, ...).
        Must be JSON-serializable — keep values to str/int/float/bool/list/dict.
    retryable:
        Hint for the orchestrator's retry policy.
    """

    #: subclasses may set a default anomaly so call sites need not repeat it
    default_anomaly: Anomaly | None = None

    def __init__(
        self,
        message: str,
        *,
        anomaly: Anomaly | None = None,
        context: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.anomaly = anomaly or self.default_anomaly
        self.context: dict[str, Any] = dict(context or {})
        self.retryable = retryable
        #: set True once written to the anomaly stream, so wrappers don't double-log
        self.logged = False

    def with_context(self, **fields: Any) -> "TendError":
        """Attach/override context fields in place and return self (for re-raise)."""
        self.context.update(fields)
        return self

    def to_record(self) -> dict[str, Any]:
        """Structured form for the anomaly/event logs."""
        return {
            "error_type": type(self).__name__,
            "message": self.message,
            "anomaly": self.anomaly.value if self.anomaly else None,
            "retryable": self.retryable,
            "context": self.context,
        }

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        tag = f"[{self.anomaly.value}] " if self.anomaly else ""
        ctx = f" {self.context}" if self.context else ""
        return f"{tag}{self.message}{ctx}"


# --------------------------------------------------------------------------- #
# Configuration / source loading
# --------------------------------------------------------------------------- #
class ConfigError(TendError):
    """Bad or missing runtime configuration (env, paths, credentials)."""


class SourceError(TendError):
    """BIRD mini-dev source could not be loaded or is malformed."""


# --------------------------------------------------------------------------- #
# LLM layer
# --------------------------------------------------------------------------- #
class LLMError(TendError):
    """Base for any fault originating from an LLM call."""

    default_anomaly = Anomaly.API_ERROR


class RateLimitError(LLMError):
    default_anomaly = Anomaly.RATE_LIMIT

    def __init__(self, message: str, **kw: Any) -> None:
        kw.setdefault("retryable", True)
        super().__init__(message, **kw)


class LLMTimeoutError(LLMError):
    default_anomaly = Anomaly.TIMEOUT

    def __init__(self, message: str, **kw: Any) -> None:
        kw.setdefault("retryable", True)
        super().__init__(message, **kw)


class EmptyResponseError(LLMError):
    default_anomaly = Anomaly.EMPTY_RESPONSE

    def __init__(self, message: str, **kw: Any) -> None:
        kw.setdefault("retryable", True)
        super().__init__(message, **kw)


class TruncatedResponseError(LLMError):
    default_anomaly = Anomaly.TRUNCATED

    def __init__(self, message: str, **kw: Any) -> None:
        kw.setdefault("retryable", True)
        super().__init__(message, **kw)


class RefusalError(LLMError):
    """Model declined to answer — usually a prompt/content issue worth surfacing."""

    default_anomaly = Anomaly.REFUSAL


class PromptAnomalyError(LLMError):
    """The *prompt we constructed* is defective (empty role, oversize, bad template).

    Distinguished from output faults because the fix is on our side, not the model's.
    """

    default_anomaly = Anomaly.PROMPT_MALFORMED


class ContextOverflowError(PromptAnomalyError):
    default_anomaly = Anomaly.CONTEXT_OVERFLOW


class ResponseParseError(LLMError):
    """Response could not be parsed into the expected structure (e.g. invalid JSON)."""

    default_anomaly = Anomaly.PARSE_ERROR

    def __init__(self, message: str, **kw: Any) -> None:
        kw.setdefault("retryable", True)
        super().__init__(message, **kw)


class SchemaValidationError(LLMError):
    """Parsed output failed JSON-Schema validation against the agent I/O contract."""

    default_anomaly = Anomaly.SCHEMA_INVALID

    def __init__(self, message: str, **kw: Any) -> None:
        kw.setdefault("retryable", True)
        super().__init__(message, **kw)


class ContractViolationError(LLMError):
    """Output is schema-valid but breaks a semantic agent contract (e.g. <5 mutations)."""

    default_anomaly = Anomaly.CONTRACT_VIOLATION

    def __init__(self, message: str, **kw: Any) -> None:
        kw.setdefault("retryable", True)
        super().__init__(message, **kw)


# --------------------------------------------------------------------------- #
# Deterministic execution / construction
# --------------------------------------------------------------------------- #
class ExecutionError(TendError):
    """MongoDB / mongosh execution failure (NormExec, witness probe, bridge run)."""

    default_anomaly = Anomaly.EXEC_ERROR


class DisabledOperatorError(TendError):
    """A query used one of the 6 banned operators ($sample/$rand/$$NOW/$out/$merge/$function)."""

    default_anomaly = Anomaly.DISABLED_OPERATOR


class GoldLockError(TendError):
    """gold MQL failed reference-anchoring or dual-path triangulation (P1)."""

    default_anomaly = Anomaly.GOLD_LOCK_FAILED


class GateError(TendError):
    """A publish gate (Gate-QB, Gate-SD, dual-bridge, ambiguity) rejected the record."""

    default_anomaly = Anomaly.GATE_FAILED


class MigrationError(TendError):
    """DM produced witness data inconsistent with the SRA schema (Gate-SD)."""

    default_anomaly = Anomaly.MIGRATION_ERROR


class SupplyExhaustedError(TendError):
    """A coverage cell is infeasible on the current db (no query-bearing supply)."""

    default_anomaly = Anomaly.SUPPLY_EXHAUSTED


class WorkflowError(TendError):
    """Orchestration-level fault (bad task graph, dependency cycle, fan-out failure)."""

    default_anomaly = Anomaly.INTERNAL


def wrap_unexpected(exc: BaseException, **context: Any) -> TendError:
    """Coerce a stray non-TendError into a typed INTERNAL anomaly for uniform logging."""
    if isinstance(exc, TendError):
        return exc.with_context(**context)
    context = {
        **context,
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }
    err = TendError(
        f"unexpected {type(exc).__name__}: {exc}",
        anomaly=Anomaly.INTERNAL,
        context=context,
    )
    err.__cause__ = exc
    return err
