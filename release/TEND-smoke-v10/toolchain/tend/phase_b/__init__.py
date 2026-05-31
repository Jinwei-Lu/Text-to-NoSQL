"""Phase B agents: construction (QPS/MS/MUT/PV) and validation (NLP/RTV/NNC/bridges/RA)."""

from tend.phase_b.derive_cfs import derive_canonical_form_set
from tend.phase_b.ms import ms_synthesize
from tend.phase_b.mut import build_mutations_payload, generate_mutations, validate_mutations
from tend.phase_b.pv import verify_properties
from tend.phase_b.qps import sample_query_plan

__all__ = [
    "derive_canonical_form_set",
    "sample_query_plan",
    "ms_synthesize",
    "generate_mutations",
    "validate_mutations",
    "build_mutations_payload",
    "verify_properties",
]

try:
    from tend.phase_b.bridges import bridge_verdict, graduated_gate, run_sql_bridge, run_template_bridge
    from tend.phase_b.nlp import paraphrase_nlq_pair
    from tend.phase_b.nnc import assess_nnc
    from tend.phase_b.ra import AUGMENT_BUDGET, audit_realism
    from tend.phase_b.rtv import rtv_verify

    __all__.extend(
        [
            "AUGMENT_BUDGET",
            "assess_nnc",
            "audit_realism",
            "bridge_verdict",
            "graduated_gate",
            "paraphrase_nlq_pair",
            "rtv_verify",
            "run_sql_bridge",
            "run_template_bridge",
        ]
    )
except ImportError:
    pass
