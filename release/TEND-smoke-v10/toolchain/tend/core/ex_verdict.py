from __future__ import annotations

from .ast_check import AST_check, disabled_operator_scanner
from .equiv import equiv_rec
from .models import CanonicalFormSet, Record
from .normexec import NormExec


def EX_verdict(q_p: str, record: dict | Record, snapshot: Any) -> bool:
    if isinstance(record, Record):
        cfs = record.canonical_form_set
        gold_mql = record.mql
    else:
        cfs = CanonicalFormSet.from_dict(record["canonical_form_set"])
        gold_mql = record["MQL"]

    if AST_check(q_p, cfs) != "pass":
        return False
    if disabled_operator_scanner(q_p):
        return False

    gold_result = NormExec(gold_mql, snapshot)
    pred_result = NormExec(q_p, snapshot)
    from tend.errors import BOT, BOT_EXEC

    if isinstance(gold_result, (BOT, BOT_EXEC)) or isinstance(pred_result, (BOT, BOT_EXEC)):
        return False
    return equiv_rec(pred_result, gold_result, order_sensitive=False)
