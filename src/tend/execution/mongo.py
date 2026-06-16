"""MongoDB execution: NormExec (run an MQL aggregate) and ``equiv_rec`` (result equality).

A :class:`MongoExecutor` loads per-db witness data into an ephemeral working database
(``<prefix><run_id>_<db_id>`` by default, shortened with a stable hash when MongoDB's
database-name limit requires it, or an existing ``<db_id>`` database when configured),
runs MQL aggregate strings, and normalizes results for the
≡_rec comparison used by MS gold-lock, PV, RTV and the NNC bridges. Connection is lazy so
stub/offline runs that never execute don't require a reachable server.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import threading
from typing import Any

from ..config import Settings
from ..errors import ExecutionError
from ..observability import RunLogger
from .ast_check import assert_no_disabled, parse_pipeline
from .signature import _canon, _FLOAT_NDIGITS

# Server-side time bound for every aggregate. Indexed gold/round-trip queries finish in
# well under a second; LLM-generated PV mutations can be pathological (e.g. an unindexed
# $lookup that COLLSCANs financial.trans's ~1M rows per parent), which without a cap hangs
# the whole run. A timed-out aggregate raises ExecutionTimeout -> ExecutionError, which PV
# correctly treats as a discriminating (result-changing) mutation.
_EXEC_MAX_TIME_MS = 30_000
_MONGO_DB_NAME_LIMIT = 63
_MONGO_DB_HASH_CHARS = 12
_MONGO_DB_SAFE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "_-"
)
_PUBLIC_SAMPLE_LIMIT = 100
_PUBLIC_PROBE_LIMIT = 100
_SHAPE_SCALAR_SAMPLE_LIMIT = 5
_SHAPE_SCALAR_STRING_LIMIT = 120
_PROBE_WORK_BOUNDARY_STAGES = frozenset(
    {
        "$bucket",
        "$bucketAuto",
        "$densify",
        "$facet",
        "$fill",
        "$graphLookup",
        "$group",
        "$lookup",
        "$setWindowFields",
        "$sort",
        "$sortByCount",
        "$unionWith",
        "$unwind",
    }
)


def _scoped_db_name(*, prefix: str, run_id: str, db_id: str) -> str:
    prefix_part = _safe_mongo_db_name_part(prefix, fallback="", trim=False)
    run_part = _safe_mongo_db_name_part(run_id, fallback="run", trim=True)
    db_part = _safe_mongo_db_name_part(db_id, fallback="db", trim=True)
    candidate = f"{prefix_part}{run_part}_{db_part}"
    if len(candidate) <= _MONGO_DB_NAME_LIMIT:
        return candidate

    digest = hashlib.sha1(f"{prefix}{run_id}_{db_id}".encode("utf-8")).hexdigest()
    digest = digest[:_MONGO_DB_HASH_CHARS]
    db_suffix = db_part[-min(len(db_part), 24):]
    fixed_suffix = f"_{digest}_{db_suffix}"
    prefix_budget = min(len(prefix_part), max(0, _MONGO_DB_NAME_LIMIT - len(fixed_suffix) - 1))
    prefix_head = prefix_part[:prefix_budget]
    run_budget = _MONGO_DB_NAME_LIMIT - len(prefix_head) - len(fixed_suffix)

    if run_budget < 1:
        prefix_head = ""
        db_budget = max(1, _MONGO_DB_NAME_LIMIT - len(f"_{digest}_") - 1)
        db_suffix = db_part[-min(len(db_part), db_budget):]
        fixed_suffix = f"_{digest}_{db_suffix}"
        run_budget = max(1, _MONGO_DB_NAME_LIMIT - len(fixed_suffix))

    return f"{prefix_head}{run_part[:run_budget]}{fixed_suffix}"


def _safe_mongo_db_name_part(value: str, *, fallback: str, trim: bool) -> str:
    safe = "".join(ch if ch in _MONGO_DB_SAFE_CHARS else "_" for ch in value)
    if trim:
        safe = safe.strip("_")
    return safe or fallback


class MongoExecutor:
    """Lazy MongoDB client wrapper for witness loading + NormExec."""

    def __init__(self, settings: Settings, logger: RunLogger) -> None:
        self._s = settings
        self._log = logger
        self._client: Any = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def _connect(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    try:
                        from pymongo import MongoClient

                        self._client = MongoClient(
                            self._s.mongo_uri,
                            serverSelectionTimeoutMS=4000,
                            # default 100 silently queues the suite's concurrent
                            # to_thread aggregations behind a connection slot
                            maxPoolSize=max(1, int(getattr(self._s, "mongo_max_pool_size", 200))),
                        )
                        self._client.admin.command("ping")
                    except Exception as exc:  # noqa: BLE001 - surfaced as a typed execution anomaly
                        raise ExecutionError("cannot connect to MongoDB",
                                             context={"uri_set": bool(self._s.mongo_uri)}) from exc
        return self._client

    def available(self) -> bool:
        try:
            self._connect()
            return True
        except ExecutionError:
            return False

    def _db_name(self, db_id: str) -> str:
        if self._s.use_existing_mongo_dbs:
            return db_id
        # Run-scoped so concurrent runs/tests never collide. MongoDB caps database names at
        # 63 bytes, so long user run IDs are compacted but remain deterministic.
        return _scoped_db_name(
            prefix=str(self._s.mongo_db_prefix),
            run_id=str(self._s.run_id),
            db_id=str(db_id),
        )

    def count(self, db_id: str, collection: str) -> int:
        client = self._connect()
        return int(client[self._db_name(db_id)][collection].estimated_document_count())

    def sample_fields(self, db_id: str, collection: str, n: int = 200) -> set[str]:
        """Union of top-level field names across the first ``n`` docs (for new-field diffing)."""
        client = self._connect()
        fields: set[str] = set()
        for doc in client[self._db_name(db_id)][collection].find({}, limit=n):
            fields.update(doc.keys())
        return fields

    def snapshot_database(self, db_id: str, sample_size: int) -> dict[str, list[dict[str, Any]]]:
        """Return a bounded JSON-safe sample from every collection in the working DB."""
        from bson import json_util

        client = self._connect()
        db = client[self._db_name(db_id)]
        out: dict[str, list[dict[str, Any]]] = {}
        for collection in sorted(db.list_collection_names()):
            docs = list(db[collection].find({}, limit=max(1, sample_size)))
            out[collection] = [
                json.loads(json_util.dumps(doc, default=str))
                for doc in docs
                if isinstance(doc, dict)
            ]
        return out

    def list_collections(self, db_id: str) -> list[dict[str, Any]]:
        """Return a bounded public summary of collection names and approximate sizes."""
        client = self._connect()
        db = client[self._db_name(db_id)]
        out: list[dict[str, Any]] = []
        for collection in sorted(db.list_collection_names()):
            try:
                count = int(db[collection].estimated_document_count())
            except Exception:  # noqa: BLE001 - fake/remote collections may not expose counts
                count = 0
            out.append({"collection": collection, "estimated_document_count": count})
        return out

    def sample_documents(
        self,
        db_id: str,
        collection: str,
        *,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Return a bounded public sample summary without raw documents."""
        sample_limit = _bounded_read_limit(limit, default=5, maximum=_PUBLIC_SAMPLE_LIMIT)
        docs = self._sample_documents_raw(db_id, collection, limit=sample_limit)
        normalized = [_normalize_doc(doc) for doc in docs]
        return {
            "ok": True,
            "db_id": db_id,
            "collection": collection,
            "limit": sample_limit,
            "sample_count": len(normalized),
            "result_shape": _summarize_documents_shape(normalized),
            "redaction": {"raw_rows": False},
        }

    def _sample_documents_raw(
        self,
        db_id: str,
        collection: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Return a bounded JSON-safe sample for internal redacted tool computation."""
        from bson import json_util

        sample_limit = _bounded_read_limit(limit, default=5, maximum=_PUBLIC_SAMPLE_LIMIT)
        client = self._connect()
        docs = client[self._db_name(db_id)][collection].find({}, limit=sample_limit)
        return [
            json.loads(json_util.dumps(doc, default=str))
            for doc in docs
            if isinstance(doc, dict)
        ]

    def run_readonly_probe(
        self,
        db_id: str,
        mql: str,
        *,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Run a bounded read-only aggregate and return a result-shape summary.

        The public probe rejects destructive/nondeterministic operators and caps both
        upstream work and final output rows. It never returns raw result rows.
        """
        assert_no_disabled(mql)
        collection, pipeline = parse_pipeline(mql)
        forced_limit = _bounded_read_limit(limit, default=20, maximum=_PUBLIC_PROBE_LIMIT)
        bounded_pipeline, work_limit_applied = _force_pipeline_limit(pipeline, forced_limit)
        client = self._connect()
        db = client[self._db_name(db_id)]
        try:
            raw = list(db[collection].aggregate(bounded_pipeline, maxTimeMS=_EXEC_MAX_TIME_MS))
        except Exception as exc:  # noqa: BLE001 - pymongo/operator errors -> typed anomaly
            raise ExecutionError(
                "readonly probe execution failed",
                context={"db_id": db_id, "collection": collection, "error": str(exc)[:300]},
            ) from exc
        docs = [_normalize_doc(doc) for doc in raw if isinstance(doc, dict)]
        return {
            "ok": True,
            "db_id": db_id,
            "collection": collection,
            "stage_count": len(pipeline),
            "bounded_stage_count": len(bounded_pipeline),
            "forced_limit": forced_limit,
            "work_limit_applied": work_limit_applied,
            "result_count": len(docs),
            "result_shape": _summarize_documents_shape(docs),
        }

    # ------------------------------------------------------------------ #
    def load_witness(self, db_id: str, collections: dict[str, list[dict[str, Any]]]) -> None:
        """Drop and reload the working db for ``db_id`` from ``{collection: [docs]}``.

        Docs may use MongoDB Extended JSON ($oid/$date/...); they are coerced via bson.
        """
        if self._s.use_existing_mongo_dbs:
            self._log.info(
                "witness_reuse_existing_db",
                db_id=db_id,
                db_name=self._db_name(db_id),
                collections={c: len(d) for c, d in collections.items()},
            )
            return
        from bson import json_util

        client = self._connect()
        db = client[self._db_name(db_id)]
        client.drop_database(self._db_name(db_id))
        indexes: dict[str, list[str]] = {}
        for coll, docs in collections.items():
            if not docs:
                continue
            coerced = json_util.loads(json.dumps(docs, default=str))
            db[coll].insert_many(coerced)
            created = self._index_join_fields(db[coll], coerced)
            if created:
                indexes[coll] = created
        self._log.info("witness_loaded", db_id=db_id,
                       collections={c: len(d) for c, d in collections.items()},
                       indexes=indexes)

    @staticmethod
    def _index_join_fields(collection: Any, docs: list[Any], *, sample: int = 200) -> list[str]:
        """Index top-level ``*_id`` join keys so ``$lookup`` foreignField scans avoid O(n·m).

        The gold-lock for join/ratio archetypes (e.g. present_missing_projection) joins a
        parent onto a large fact collection on its foreign key; on financial.trans (~1M
        rows) an unindexed foreignField turns each record's lock into a full scan. Indexes
        are a pure performance optimization — never correctness-affecting and never fatal.
        """
        candidates: set[str] = set()
        for doc in docs[:sample]:
            if not isinstance(doc, dict):
                continue
            for key, value in doc.items():
                if key != "_id" and key.endswith("_id") and not isinstance(value, (dict, list)):
                    candidates.add(key)
        created: list[str] = []
        for field in sorted(candidates):
            try:
                collection.create_index(field)
                created.append(field)
            except Exception:  # noqa: BLE001 - index build is best-effort, never fatal
                pass
        return created

    def norm_exec(self, db_id: str, mql: str) -> list[dict[str, Any]]:
        """Execute an MQL aggregate and return the *normalized* result documents."""
        assert_no_disabled(mql)
        collection, pipeline = parse_pipeline(mql)
        client = self._connect()
        db = client[self._db_name(db_id)]
        try:
            raw = list(db[collection].aggregate(pipeline, maxTimeMS=_EXEC_MAX_TIME_MS))
        except Exception as exc:  # noqa: BLE001 - pymongo/operator errors -> typed anomaly
            raise ExecutionError("aggregate execution failed",
                                 context={"db_id": db_id, "collection": collection,
                                          "error": str(exc)[:300]}) from exc
        return [_normalize_doc(d) for d in raw]

    def raw_database(self, db_id: str) -> Any:
        """Raw pymongo ``Database`` handle for the working db (read-only use).

        Respects the same ``_db_name`` scoping as every other accessor
        (``use_existing_mongo_dbs`` vs run-scoped names), so induction, probes,
        and execution all hit the physical database that ``norm_exec`` uses.
        """
        return self._connect()[self._db_name(db_id)]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _normalize_doc(doc: Any) -> Any:
    """Norm layers (01 §01-4): recurse, unify numerics, keep null-vs-missing intact.

    ``_id`` is **kept** — it carries meaning as a semantic ``$group`` key (per-subtype agg,
    polymorphic dispatch) and as the preserved root key; dropping it would mis-compare those
    records. BSON scalars from existing MongoDB databases are converted into deterministic
    Extended JSON values so evaluation hashing/reporting remains JSON-safe.
    """
    if isinstance(doc, dict):
        return {str(k): _normalize_doc(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_normalize_doc(v) for v in doc]
    unified = _unify_number(doc)
    if unified is None or isinstance(unified, (str, int, bool)):
        return unified
    return _canon(unified)


def _unify_number(v: Any) -> Any:
    """Collapse integral floats to int (gold ``1`` == predicted ``1.0``); round others."""
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        if v.is_integer():
            return int(v)
        return round(v, _FLOAT_NDIGITS)
    return v


def _bounded_read_limit(limit: int | None, *, default: int, maximum: int) -> int:
    try:
        parsed = int(limit) if limit is not None else default
    except (TypeError, ValueError):
        parsed = default
    if parsed <= 0:
        parsed = default
    return min(parsed, maximum)


def _force_pipeline_limit(pipeline: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], bool]:
    bounded: list[dict[str, Any]] = []
    saw_limit = False
    has_upstream_limit = False
    inserted_work_limit = False
    for index, stage in enumerate(pipeline):
        if not isinstance(stage, dict):
            if index == 0:
                bounded.append({"$limit": limit})
                has_upstream_limit = True
                inserted_work_limit = True
            bounded.append(stage)
            continue
        copied = dict(stage)
        if not has_upstream_limit and not _is_probe_initial_scan_safe_stage(copied):
            bounded.append({"$limit": limit})
            has_upstream_limit = True
            inserted_work_limit = True
        if not has_upstream_limit and _is_probe_work_boundary_stage(copied):
            bounded.append({"$limit": limit})
            has_upstream_limit = True
            inserted_work_limit = True
        if "$limit" in copied:
            saw_limit = True
            copied["$limit"] = _bounded_read_limit(
                copied.get("$limit"),
                default=limit,
                maximum=limit,
            )
            has_upstream_limit = True
        bounded.append(copied)
    if not saw_limit:
        bounded.append({"$limit": limit})
    return bounded, inserted_work_limit


def _is_probe_work_boundary_stage(stage: dict[str, Any]) -> bool:
    return any(operator in stage for operator in _PROBE_WORK_BOUNDARY_STAGES)


def _is_probe_initial_scan_safe_stage(stage: dict[str, Any]) -> bool:
    return "$match" in stage or "$limit" in stage


def _summarize_documents_shape(docs: list[dict[str, Any]]) -> dict[str, Any]:
    paths: dict[str, Counter[str]] = {}
    samples: dict[str, list[Any]] = {}
    for doc in docs:
        _walk_doc_shape(doc, (), paths, samples)
    return {
        "document_count": len(docs),
        "paths": {
            path: {
                "type_counts": dict(sorted(counts.items())),
                **({"scalar_samples": samples[path]} if path in samples else {}),
            }
            for path, counts in sorted(paths.items())
        },
    }


def _walk_doc_shape(
    value: Any,
    path: tuple[str, ...],
    paths: dict[str, Counter[str]],
    samples: dict[str, list[Any]],
) -> None:
    if isinstance(value, dict):
        if path:
            paths.setdefault(_shape_path(path), Counter())["object"] += 1
        for key, child in value.items():
            _walk_doc_shape(child, path + (str(key),), paths, samples)
        return
    if isinstance(value, list):
        if path:
            paths.setdefault(_shape_path(path), Counter())["array"] += 1
        for item in value:
            if isinstance(item, dict):
                _walk_doc_shape(item, path + ("[]",), paths, samples)
            else:
                sample_path = _shape_path(path + ("[]",))
                paths.setdefault(sample_path, Counter())[_shape_kind(item)] += 1
                _add_shape_scalar_sample(samples, sample_path, item)
        return
    if path:
        sample_path = _shape_path(path)
        paths.setdefault(sample_path, Counter())[_shape_kind(value)] += 1
        _add_shape_scalar_sample(samples, sample_path, value)


def _add_shape_scalar_sample(samples: dict[str, list[Any]], path: str, value: Any) -> None:
    if not _shape_path_allows_scalar_sample(path):
        return
    if not isinstance(value, str | int | float | bool) or value is None:
        return
    sample: Any
    if isinstance(value, str):
        if not value or len(value) > _SHAPE_SCALAR_STRING_LIMIT:
            return
        sample = value
    else:
        sample = value
    values = samples.setdefault(path, [])
    if sample in values or len(values) >= _SHAPE_SCALAR_SAMPLE_LIMIT:
        return
    values.append(sample)


def _shape_path_allows_scalar_sample(path: str) -> bool:
    leaf = path.rsplit(".", 1)[-1].replace("[]", "")
    if leaf in {"field_name", "field_type", "key", "keys", "k", "type"}:
        return True
    return leaf.endswith(("_key", "_keys", "_field", "_fields"))


def _shape_path(path: tuple[str, ...]) -> str:
    out: list[str] = []
    for part in path:
        if part == "[]":
            if out:
                out[-1] += "[]"
            else:
                out.append("[]")
        else:
            out.append(part)
    return ".".join(out)


def _shape_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return type(value).__name__


def _doc_key(doc: Any) -> str:
    return json.dumps(_canon(doc), ensure_ascii=False, sort_keys=True, default=str)


def equiv_rec(left: list[dict[str, Any]], right: list[dict[str, Any]], *,
              order_sensitive: bool) -> bool:
    """≡_rec: record-set equivalence. Order-insensitive compares as a multiset.

    Field-name-sensitive: two rows are equal only when they carry the same keys *and* values.
    Used by the ``EVM`` diagnostic (strict "names + values" check), not by the headline ``EX``.
    """
    if left is None or right is None:  # defensive: callers should guard upstream, but keep safe
        return False
    if len(left) != len(right):
        return False
    if order_sensitive:
        return all(_doc_key(a) == _doc_key(b) for a, b in zip(left, right))
    return sorted(_doc_key(d) for d in left) == sorted(_doc_key(d) for d in right)


def _row_values_key(row: Any) -> str:
    """Canonical key of a row's VALUES, ignoring its TOP-LEVEL field NAMES.

    A row collapses to the sorted multiset of its top-level (recursively canonicalized) values, so
    two rows are equal when they hold the same values under any output *column* labels.

    Scope of name-insensitivity — read carefully, this is the EX contract: only the row's TOP-LEVEL
    keys are treated as cosmetic column labels and dropped. A value that is itself a nested document
    keeps its keys, because :func:`_doc_key` serializes nested objects key-and-value. That is
    deliberate: nested field names carry semantic identity (in ``{"min": 1, "max": 5}`` the keys say
    which value is the min and which the max), so flattening them away would make ``{"min":1,"max":5}``
    and ``{"min":5,"max":1}`` compare equal — a false positive, the cardinal sin for a benchmark
    oracle. Hence when an NLQ requests an array-of-objects output, a prediction must reproduce the
    nested object's field names, not merely its values. Top-level output column names remain free.
    """
    if isinstance(row, dict):
        return json.dumps(sorted(_doc_key(v) for v in row.values()), ensure_ascii=False)
    return _doc_key(row)


# Public alias: the evaluator's graded EXF1 metric and outcome decomposition need the same
# row identity (top-level-name-insensitive, nested-key-sensitive) as the ≡_val predicates.
row_values_key = _row_values_key


def equiv_rec_values(left: list[dict[str, Any]], right: list[dict[str, Any]], *,
                     order_sensitive: bool) -> bool:
    """≡_val: result-set equivalence IGNORING output field names.

    This is the Text-to-SQL/NoSQL execution-accuracy convention (Spider/BIRD compare result
    tables by value, not by output column name): a prediction that computes the right answer is
    correct regardless of how it labels its output columns. Backs the headline ``EX`` metric.
    Order-insensitive compares as a multiset; column naming is graded separately by ``EFM``.

    Name-insensitivity is TOP-LEVEL ONLY (see :func:`_row_values_key`): the per-row output *column*
    labels are ignored, but the internal structure of a nested document value — including its nested
    field names — is significant, because those names encode which value is which. A gold that emits
    an array-of-objects therefore defines a nested-key contract the prediction must match; keep gold
    nested key names faithful to the NLQ wording so that contract is fair.
    """
    if left is None or right is None:
        return False
    if len(left) != len(right):
        return False
    left_keys = [_row_values_key(row) for row in left]
    right_keys = [_row_values_key(row) for row in right]
    if order_sensitive:
        return left_keys == right_keys
    return sorted(left_keys) == sorted(right_keys)


def _row_values_counter(row: Any) -> Counter[str]:
    """Multiset of a row's top-level (recursively canonicalized) values; see _row_values_key."""
    if isinstance(row, dict):
        return Counter(_doc_key(v) for v in row.values())
    return Counter([_doc_key(row)])


def _is_value_submultiset(gold: Counter[str], pred: Counter[str]) -> bool:
    """True when every gold value (with multiplicity) is present in the predicted row."""
    return all(pred.get(value, 0) >= count for value, count in gold.items())


def _row_surplus(gold: Counter[str], pred: Counter[str]) -> int:
    """Predicted row's extra value count over gold (== extra top-level columns)."""
    return sum(pred.values()) - sum(gold.values())


def _row_pair_matches(gold: Counter[str], pred: Counter[str], max_surplus: int | None) -> bool:
    """Gold row covered by the predicted row, within the tolerated column surplus."""
    if max_surplus is not None and _row_surplus(gold, pred) > max_surplus:
        return False
    return _is_value_submultiset(gold, pred)


def _has_perfect_subset_matching(
    gold: list[Counter[str]], pred: list[Counter[str]], max_surplus: int | None
) -> bool:
    """Each gold row matches a DISTINCT predicted row whose values superset it (Kuhn's algo)."""
    adjacency = [
        [
            j
            for j, pred_row in enumerate(pred)
            if _row_pair_matches(gold_row, pred_row, max_surplus)
        ]
        for gold_row in gold
    ]
    match_pred_to_gold = [-1] * len(pred)

    def _augment(gold_index: int, seen: list[bool]) -> bool:
        for pred_index in adjacency[gold_index]:
            if seen[pred_index]:
                continue
            seen[pred_index] = True
            owner = match_pred_to_gold[pred_index]
            if owner == -1 or _augment(owner, seen):
                match_pred_to_gold[pred_index] = gold_index
                return True
        return False

    for gold_index in range(len(gold)):
        if not _augment(gold_index, [False] * len(pred)):
            return False
    return True


def equiv_rec_values_superset(
    gold: list[dict[str, Any]],
    predicted: list[dict[str, Any]],
    *,
    order_sensitive: bool,
    max_surplus: int | None = None,
) -> bool:
    """Column-tolerant execution match: a prediction is correct when every requested gold
    value is present, even if the prediction carries SUPERFLUOUS extra top-level columns.

    ``max_surplus=None`` reproduces the Spider 2.0 convention exactly — execution accuracy
    "tolerates superfluous columns in the SELECT clause provided all requested columns are
    present and the core answer matches" — with UNLIMITED surplus (the ``EXC_spider``
    diagnostic). A bounded ``max_surplus`` caps the per-row extra value count and backs the
    headline ``EXC`` metric: MongoDB aggregation has exactly two mechanical surplus channels
    (a leftover synthetic ``$group`` ``_id`` and a retained helper/sort key), so β=2 forgives
    pipeline mechanics while a projection-free document dump (surplus ≥ 3) — which never
    performed the requested answer extraction and inflates accidental value collisions —
    still fails. No ``_id`` key-name special case: top-level name-insensitivity stays uniform.

    Versus the strict ``EX`` (:func:`equiv_rec_values`, exact value multiset), this only
    relaxes the TOP-LEVEL column count: each gold row's value multiset must be a SUB-multiset
    of the aligned prediction row's, with at most ``max_surplus`` extra values. Row count and
    ordering (when gold is ``$sort``-ed) are still enforced, and nested object keys remain
    significant -- so a wrong value, a missing requested value, an extra row, or a reordered
    ranked result still fails. ``gold`` must be the FIRST argument (the relation is asymmetric).
    """
    if gold is None or predicted is None:
        return False
    if len(gold) != len(predicted):
        return False
    gold_counters = [_row_values_counter(row) for row in gold]
    pred_counters = [_row_values_counter(row) for row in predicted]
    if order_sensitive:
        return all(
            _row_pair_matches(g, p, max_surplus)
            for g, p in zip(gold_counters, pred_counters)
        )
    return _has_perfect_subset_matching(gold_counters, pred_counters, max_surplus)
