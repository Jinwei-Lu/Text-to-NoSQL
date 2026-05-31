"""Central exception types for the TEND pipeline."""


class TENDError(Exception):
    """Base exception for TEND pipeline errors."""


class SplitError(TENDError):
    """Cross-domain split constraint violation."""


class MSSynthesisError(TENDError):
    """MS dual-path synthesis divergence."""


class TriggerAmbiguityError(TENDError):
    """NNC ambiguity attack failed to converge."""


class VariantCoverageError(TENDError):
    """Schema variant coverage insufficient."""


class EmptyVariantError(TENDError):
    """Required schema variant produced no documents."""


class BridgeGateFail(TENDError):
    """Graduated dual-bridge gate failure."""


class DisjointnessViolation(TENDError):
    """LLM pool disjointness invariant violated."""


class CoverageInfeasible(TENDError):
    """Coverage controller cannot satisfy quotas."""


class RetryBudgetExhausted(TENDError):
    """Agent reflow retry budget exceeded."""


class BudgetExceeded(TENDError):
    """Per-pool LLM cost budget exceeded."""


class BOT(TENDError):
    """Parse failure sentinel."""


class BOT_EXEC(TENDError):
    """Execution failure sentinel."""
