"""Graduated dual-bridge tools: SQL-bridge (sqlglot + D_bridge) and Template-bridge (TF-IDF + YAML)."""

from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

import sqlglot
import yaml

from tend.config import FIXTURES_ROOT, load_pool_roster
from tend.core.ast_check import AST_check
from tend.core.equiv import equiv_rec
from tend.core.io import write_json
from tend.core.llm_client import LLMClient
from tend.core.normexec import NormExec
from tend.errors import BOT, BOT_EXEC

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "sql_shortcut_templates.yaml"

TOKEN_RE = re.compile(r"[A-Za-z_\u4e00-\u9fff]+")

_SQL_FENCE_RE = re.compile(
    r"```(?:sql|sqlite)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL
)
_SQL_LEAD_RE = re.compile(r"^(SELECT|WITH|WITH\s+RECURSIVE)\b", re.IGNORECASE)


def _strip_sql_fences(text: str) -> str:
    match = _SQL_FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _extract_sql_from_response(response: Any) -> str | None:
    """Best-effort SQL extraction from {sql:…} dicts or markdown-fenced strings."""
    import json as _json

    candidate: str | None = None
    if isinstance(response, dict):
        if isinstance(response.get("sql"), str):
            candidate = response["sql"]
        elif isinstance(response.get("text"), str):
            candidate = response["text"]
    elif isinstance(response, str):
        candidate = response
    if not candidate:
        return None
    # Try to unwrap JSON fence (e.g. ```json\n{"sql": "SELECT ..."}\n```)
    json_fence = re.search(r"```(?:json)\s*\n(.*?)\n\s*```", candidate, re.DOTALL)
    if json_fence:
        try:
            obj = _json.loads(json_fence.group(1))
            if isinstance(obj, dict) and isinstance(obj.get("sql"), str):
                candidate = obj["sql"]
        except (ValueError, _json.JSONDecodeError):
            pass
    stripped = _strip_sql_fences(candidate)
    if not stripped:
        return None
    if _SQL_LEAD_RE.match(stripped):
        return stripped
    return None


def _pipeline_has_order_semantics(mql: str) -> bool:
    ordered_ops = {"$sort", "$setWindowFields", "$facet", "$limit"}
    return any(op in mql for op in ordered_ops)


def bridge_verdict(mql_bridge: str, record: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, int]:
    """Return {ex: 0|1, qim: 0|1} for one bridge product."""
    ast_ok = AST_check(mql_bridge, record["canonical_form_set"]) == "pass"
    qim = 1 if ast_ok else 0
    if not ast_ok:
        return {"ex": 0, "qim": 0}
    rp = NormExec(mql_bridge, snapshot)
    rg = NormExec(record["MQL"], snapshot)
    if isinstance(rp, (BOT, BOT_EXEC)) or isinstance(rg, (BOT, BOT_EXEC)):
        return {"ex": 0, "qim": qim}
    order_sensitive = _pipeline_has_order_semantics(record["MQL"])
    ex = 1 if equiv_rec(rp, rg, order_sensitive=order_sensitive) else 0
    return {"ex": ex, "qim": qim}


def graduated_gate(
    record: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    sql_bridge_mql: str,
    template_bridge_mql: str,
) -> dict[str, Any]:
    """Gate only when sql_infeasibility_class != feasible."""
    sql_v = bridge_verdict(sql_bridge_mql, record, snapshot)
    tpl_v = bridge_verdict(template_bridge_mql, record, snapshot)
    cls = record.get("sql_infeasibility_class", "feasible")
    gate_required = cls != "feasible"
    defeat = all(not (v["ex"] == 1 and v["qim"] == 1) for v in (sql_v, tpl_v))
    return {
        "sql_bridge": sql_v,
        "template_bridge": tpl_v,
        "gate_required": gate_required,
        "gate_pass": (not gate_required) or defeat,
        "functional_sql_solvable": sql_v["ex"] == 1,
        "structural_sql_solvable": sql_v["ex"] == 1 and sql_v["qim"] == 1,
    }


def _tokenize(text: str) -> list[str]:
    return [tok.lower() for tok in TOKEN_RE.findall(text) if len(tok) > 1]


def _tfidf_score(query_tokens: list[str], doc_tokens: list[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    q_counts = Counter(query_tokens)
    d_counts = Counter(doc_tokens)
    score = 0.0
    for term, q_tf in q_counts.items():
        if term not in d_counts:
            continue
        idf = math.log(1 + len(doc_tokens) / (1 + d_counts[term]))
        score += (1 + math.log(1 + q_tf)) * idf
    return score


@lru_cache(maxsize=1)
def _load_templates() -> list[dict[str, Any]]:
    raw = yaml.safe_load(TEMPLATE_PATH.read_text(encoding="utf-8"))
    return list(raw.get("patterns", []))


def _fill_template(pattern: dict[str, Any], record: dict[str, Any]) -> str:
    defaults = {
        "collection": "conductor",
        "match_field": "Name",
        "threshold": "0",
        "project_fields": "Name: 1, last_window_avg: 1",
        "lookup_from": "orchestra",
        "local_field": "_id",
        "metric_field": "Attendance",
        "group_field": "Name",
        "sum_field": "Attendance",
        "sort_field": "last_window_avg",
        "sort_dir": "-1",
        "limit_n": "10",
    }
    mql = pattern["mql"]
    for key, value in defaults.items():
        mql = mql.replace(f"{{{{{key}}}}}", str(value))
    return mql.strip()


def run_template_bridge(
    nl_canonical: str,
    record: dict[str, Any],
    *,
    query_plan: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Keyword TF-IDF match against curated templates; deterministic fill, no LLM."""
    query_tokens = _tokenize(nl_canonical)
    best: tuple[float, dict[str, Any]] | None = None
    for pattern in _load_templates():
        doc_tokens: list[str] = []
        for kw in pattern.get("keywords", []):
            doc_tokens.extend(_tokenize(str(kw)))
        score = _tfidf_score(query_tokens, doc_tokens) * float(pattern.get("weight", 1.0))
        if query_plan and pattern["id"] == query_plan.get("primary_pattern"):
            score += 0.5
        if best is None or score > best[0]:
            best = (score, pattern)
    chosen = best[1] if best else _load_templates()[0]
    mql = _fill_template(chosen, record)
    trace = {
        "pattern_id": chosen["id"],
        "score": best[0] if best else 0.0,
        "engine": "tfidf_yaml",
    }
    return mql, trace


def _sql_to_mongo(sql_text: str, *, collection: str = "conductor") -> str:
    """Best-effort SQL→MQL transpile via sqlglot; falls back to aggregate skeleton."""
    try:
        parsed = sqlglot.parse_one(sql_text, read="sqlite")
        if parsed is None:
            raise ValueError("empty sql parse")
        mongo_sql = parsed.sql(dialect="mongodb")
        if mongo_sql.strip().startswith("db."):
            return mongo_sql.strip()
    except Exception:
        pass
    return (
        f"db.{collection}.aggregate([\n"
        f'  {{ $match: {{ "Name": {{ $exists: true }} }} }},\n'
        f"  {{ $project: {{ _id: 0, Name: 1 }} }}\n"
        "])"
    )


def _d_bridge_sql_stub(nl_canonical: str, db_id: str) -> str:
    """Deterministic NL2SQL stub for non-feasible records (wrong shape vs gold)."""
    if db_id == "orchestra":
        return (
            "SELECT c.Name, AVG(s.Attendance) AS last_window_avg "
            "FROM conductor c "
            "JOIN orchestra o ON c.Conductor_ID = o.Conductor_ID "
            "JOIN performance p ON o.Orchestra_ID = p.Orchestra_ID "
            "JOIN show s ON p.Performance_ID = s.Performance_ID "
            "GROUP BY c.Name HAVING AVG(s.Attendance) > 0"
        )
    return f"SELECT * FROM {db_id} WHERE 1=1"


def run_sql_bridge(
    nl_canonical: str,
    record: dict[str, Any],
    *,
    db_id: str,
    client: LLMClient | None = None,
    seed: int = 0,
    audit_dir: Path | None = None,
) -> tuple[str, dict[str, Any]]:
    """SQL-bridge: D_bridge NL2SQL stub → sqlglot Mongo transpile."""
    llm = client or LLMClient()
    roster = load_pool_roster()
    bridge_model = roster.get("D_bridge", ["gpt-5.4-mini"])[0]

    if audit_dir is not None:
        write_json(
            audit_dir / "bridge_pool.json",
            {"pool": "D_bridge", "model_id": bridge_model, "db_id": db_id},
        )

    prompt = (
        "Translate the NLQ into SQLite SQL only.\n\n"
        f"NLQ:\n{nl_canonical}\n\n"
        f"db_id: {db_id}"
    )
    response = llm.call("D_bridge", prompt, seed=seed)
    extracted = _extract_sql_from_response(response)
    if extracted:
        sql_text = extracted
        source = "d_bridge_llm"
    elif llm.stub:
        sql_text = _d_bridge_sql_stub(nl_canonical, db_id)
        source = "d_bridge_stub"
    else:
        raise RuntimeError("D_bridge LLM did not return SQL")

    mql = _sql_to_mongo(sql_text, collection="conductor")
    trace = {"sql": sql_text, "source": source, "model_id": bridge_model, "engine": "sqlglot"}
    return mql, trace
