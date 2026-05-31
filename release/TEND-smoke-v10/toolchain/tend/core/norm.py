from __future__ import annotations

from typing import Any

from .equiv import Norm as _norm_impl


def Norm(raw: Any, *, gold_mql: str = "", shape_policy: str = "preserve") -> Any:
    return _norm_impl(raw, gold_mql=gold_mql, shape_policy=shape_policy)
