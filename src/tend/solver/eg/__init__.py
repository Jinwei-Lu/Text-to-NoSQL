"""SMART-EG provider-native solver runtime."""

from .contracts import (
    ExecutionTrace,
    GateViolation,
    QueryCandidate,
    SmartEGBudgets,
    SmartEGCounters,
    SmartEGFailure,
    SmartEGPrediction,
    SmartEGState,
    SubmitGateResult,
    ToolObservation,
)
from .evidence import EvidenceClaim, EvidenceDebt, EvidenceLedger, EvidenceRecord
from .history import SmartEGHistory
from .mongo_tools import SmartEGMongoTools, SmartEgMongoTools
from .policy import ConvergenceResult, SmartEGConvergenceChecker, SmartEGPolicy
from .runtime import smart_solve_nlq_db_eg, smart_solve_record_eg
from .tools import SmartEGToolAPI

__all__ = [
    "ConvergenceResult",
    "EvidenceClaim",
    "EvidenceDebt",
    "EvidenceLedger",
    "EvidenceRecord",
    "ExecutionTrace",
    "GateViolation",
    "QueryCandidate",
    "SmartEGBudgets",
    "SmartEGConvergenceChecker",
    "SmartEGCounters",
    "SmartEGFailure",
    "SmartEGHistory",
    "SmartEGMongoTools",
    "SmartEGPolicy",
    "SmartEGPrediction",
    "SmartEGState",
    "SmartEGToolAPI",
    "SmartEgMongoTools",
    "SubmitGateResult",
    "ToolObservation",
    "smart_solve_nlq_db_eg",
    "smart_solve_record_eg",
]
