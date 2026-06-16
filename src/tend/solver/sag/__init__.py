"""SAG — Schema-as-Data Grounding solver.

The reference TEND solver. Mechanism (measured: financial EX 61/110 vs fair ReAct
4-25/110; toxicology 51, superhero 39; alignment gate 0 false positives on all
1210 golds — see ``docs/sag_solver_experiments_2026-06.md``):

1. **Induced hypothesis space** — the complete data-induced lattice path card
   (dynamic-key maps collapsed to ``<*>``) is handed to the decoder; structure
   discovery is deterministic perception, not in-loop reasoning (``induction.py``).
2. **Value-witness anchoring** — NLQ literals located in the actual data, with
   exact stored forms, including dynamic-map KEYS (``witness.py``).
3. **Two-sided alignment gate + execution-grounded repair** — A_path ∧ A_value,
   witnessed-edge $lookup admissibility, the limit contract, empty-result prefix
   bisection, and the synthetic-_id contract (``gates.py``/``repair.py``).
4. **Result-space consistency** — k=3 sampled attempts clustered by result
   equivalence; the largest cluster wins (``runtime.py``).

The solver contract is ``NLQ + read-only world``; gold fields never enter.
"""
from .induction import GroundingIndex, LatticeNode, build_grounding_index
from .runtime import (
    GroundingIndexCache,
    SAGFailure,
    SAGPolicy,
    SAGPrediction,
    sag_solve_nlq_db,
    sag_solve_record,
)
from .world import LocalWorld, MongoWorld, WorldAccess

__all__ = [
    "GroundingIndex",
    "GroundingIndexCache",
    "LatticeNode",
    "LocalWorld",
    "MongoWorld",
    "SAGFailure",
    "SAGPolicy",
    "SAGPrediction",
    "WorldAccess",
    "build_grounding_index",
    "sag_solve_nlq_db",
    "sag_solve_record",
]
