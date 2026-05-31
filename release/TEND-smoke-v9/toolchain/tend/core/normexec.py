from __future__ import annotations

from typing import Any

from tend.errors import BOT, BOT_EXEC

from .exec import Exec, MongoSession
from .norm import Norm
from .parse import Parse


def NormExec(q: str, snapshot: Any) -> Any | BOT | BOT_EXEC:
    ast = Parse(q)
    if isinstance(ast, BOT):
        return BOT
    if isinstance(snapshot, MongoSession):
        result = snapshot.exec_query(q)
    elif isinstance(snapshot, dict):
        with MongoSession("normexec", snapshot) as session:
            result = session.exec_query(q)
    else:
        return BOT_EXEC("invalid_snapshot")
    if isinstance(result, BOT_EXEC):
        return result
    return Norm(result, shape_policy="reshape")
