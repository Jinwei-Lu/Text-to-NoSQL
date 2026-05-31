from __future__ import annotations

from typing import Any

from .mql import (
    contains_forbidden_operator,
    extract_operator_tokens,
    extract_root_stage_tokens,
    parse_ok,
)
from .models import CanonicalFormSet


def AST_check(q: str, cfs: dict | CanonicalFormSet) -> str:
    if isinstance(cfs, dict):
        cfs = CanonicalFormSet.from_dict(cfs)
    if not parse_ok(q):
        return "fail:parse_error"
    tokens_all = set(extract_operator_tokens(q))
    tokens_root = set(extract_root_stage_tokens(q))
    for token in cfs.must_contain:
        if token not in tokens_all:
            return f"fail:missing:{token}"
    for token in cfs.must_contain_at_root:
        if token not in tokens_root:
            return f"fail:missing_at_root:{token}"
    for token in cfs.must_not_contain:
        if token in tokens_all:
            return f"fail:forbidden:{token}"
    for token in cfs.must_not_contain_at_root:
        if token in tokens_root:
            return f"fail:forbidden_at_root:{token}"
    return "pass"


def disabled_operator_scanner(q: str) -> bool:
    return contains_forbidden_operator(q)
