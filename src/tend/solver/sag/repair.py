"""Execution-grounded repair gradient: prefix bisection + output contracts.

When a gate-clean pipeline executes to an empty result, "0 rows" is a gradient-free
signal. Prefix bisection executes ``pipeline[:k] + [$count]`` to locate the stage
where the row count first collapses to zero (or errors) and describes it with the
ACTUAL distinct values at the filtered paths — dense, data-grounded feedback the
model can act on. The synthetic ``_id`` contract catches release-world internal
document keys (``"db::42"``) leaking into result rows.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import TYPE_CHECKING, Any

from ...errors import ExecutionError, ResultResourceUnavailableError
from ...execution.mongo import _normalize_doc, row_values_key
from ...execution.signature import canonical_json
from .gates import dyn_prefixed
from .world import WorldAccess

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .induction import GroundingIndex

_STAGE_PREVIEW_CHARS = 140
_DISTINCT_PREVIEW_CHARS = 220
_SYNTHETIC_ID_SCAN_ROWS = 5

# The no-Gate/Repair arm may emit an unbounded aggregate such as ``aggregate([])``.
# Its result-space vote therefore uses an exact disk-backed multiset instead of a Python
# list.  These constants are part of the experiment receipt/profile; changing any of
# them changes the runtime resource contract even though it does not change MQL or prompts.
EXACT_RESULT_FINGERPRINT_SCHEMA = "tend.exact_result_multiset.v1"
EXACT_RESULT_FINGERPRINT_BACKEND = "sqlite_value_key_multiset_v1"
EXACT_RESULT_SQLITE_CACHE_KIB = 2_048
EXACT_RESULT_PREVIEW_UTF8_BYTES = 2_000
# 10_000 fail-stopped financial/3458765 on 2026-08-16: gold has 46,988 rows
# (all-month unwind 53,995). 100_000 still bounds pathological cartesian dumps.
EXACT_RESULT_MAX_ROWS = 100_000
REPAIR_RESULT_PREVIEW_ROWS = 5


@dataclass(slots=True)
class ExactResultFingerprint:
    """Disk-backed exact order-insensitive ``equiv_rec_values`` evidence.

    Each normalized row is reduced to :func:`row_values_key`, exactly the identity used
    by ``equiv_rec_values(..., order_sensitive=False)``.  SQLite stores the key and its
    multiplicity, so equality is checked with a symmetric relational difference rather
    than trusting a digest.  Python retains only one BSON row/key at a time; total row
    count affects spill-disk usage, not resident result-list memory.
    """

    path: Path
    row_count: int
    digest: str
    preview: str
    spill_bytes: int
    stream_source: str
    _closed: bool = False

    def __len__(self) -> int:
        return self.row_count

    @property
    def empty(self) -> bool:
        return self.row_count == 0

    def equivalent(self, other: "ExactResultFingerprint") -> bool:
        """Return exact value-multiset equality, including duplicate multiplicity."""

        if self._closed or other._closed:
            raise ResultResourceUnavailableError(
                "exact result fingerprint was closed before comparison",
                context={"backend": EXACT_RESULT_FINGERPRINT_BACKEND},
            )
        if self.row_count != other.row_count or self.digest != other.digest:
            return False
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(str(self.path))
            connection.execute("ATTACH DATABASE ? AS rhs", (str(other.path),))
            mismatch = connection.execute(
                """
                SELECT 1 FROM (
                    SELECT value_key, multiplicity FROM value_counts
                    EXCEPT
                    SELECT value_key, multiplicity FROM rhs.value_counts
                )
                UNION ALL
                SELECT 1 FROM (
                    SELECT value_key, multiplicity FROM rhs.value_counts
                    EXCEPT
                    SELECT value_key, multiplicity FROM value_counts
                )
                LIMIT 1
                """
            ).fetchone()
            return mismatch is None
        except (OSError, sqlite3.Error) as exc:
            raise ResultResourceUnavailableError(
                "cannot compare exact result fingerprints",
                context={
                    "backend": EXACT_RESULT_FINGERPRINT_BACKEND,
                    "error": str(exc)[:300],
                },
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def receipt(self) -> dict[str, Any]:
        return {
            "schema": EXACT_RESULT_FINGERPRINT_SCHEMA,
            "backend": EXACT_RESULT_FINGERPRINT_BACKEND,
            "row_identity": "row_values_key_top_level_names_ignored_nested_names_kept",
            "order_sensitive": False,
            "multiplicity_preserved": True,
            "comparison": "exact_symmetric_relational_difference",
            "digest_used_as_prefilter_only": True,
            "row_count": self.row_count,
            "sha256": self.digest,
            "spill_bytes": self.spill_bytes,
            "sqlite_cache_kib": EXACT_RESULT_SQLITE_CACHE_KIB,
            "preview_utf8_bytes": EXACT_RESULT_PREVIEW_UTF8_BYTES,
            "stream_source": self.stream_source,
            "retained_result_rows_in_python": 0,
            "row_limit": EXACT_RESULT_MAX_ROWS,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            # Cleanup cannot change an already-made scientific selection.  Formal
            # campaigns isolate spills in their process temp area and reclaim them at
            # process exit; inability to unlink is still visible through OS telemetry.
            pass

    def __del__(self) -> None:  # pragma: no cover - best-effort crash hygiene
        try:
            self.close()
        except Exception:
            pass


def _exact_result_digest(connection: sqlite3.Connection, row_count: int) -> str:
    digest = hashlib.sha256()
    digest.update(EXACT_RESULT_FINGERPRINT_SCHEMA.encode("ascii"))
    digest.update(int(row_count).to_bytes(16, "big", signed=False))
    for value_key, multiplicity in connection.execute(
        "SELECT value_key, multiplicity FROM value_counts ORDER BY value_key"
    ):
        encoded = str(value_key).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big", signed=False))
        digest.update(encoded)
        digest.update(int(multiplicity).to_bytes(16, "big", signed=False))
    return "sha256:" + digest.hexdigest()


def build_exact_result_fingerprint(
    rows: Any,
    *,
    spill_dir: Path | None = None,
    stream_source: str = "iterable",
    max_rows: int = EXACT_RESULT_MAX_ROWS,
) -> ExactResultFingerprint:
    """Consume normalized/result documents once into an exact bounded-memory spill."""

    path: Path | None = None
    connection: sqlite3.Connection | None = None
    completed = False
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix="tend-result-multiset-",
            suffix=".sqlite3",
            dir=str(spill_dir) if spill_dir is not None else None,
        )
        os.close(descriptor)
        path = Path(raw_path)
        connection = sqlite3.connect(str(path))
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute(f"PRAGMA cache_size=-{EXACT_RESULT_SQLITE_CACHE_KIB}")
        connection.execute(
            "CREATE TABLE value_counts ("
            "value_key TEXT PRIMARY KEY NOT NULL, "
            "multiplicity INTEGER NOT NULL CHECK (multiplicity > 0)) WITHOUT ROWID"
        )
        row_count = 0
        preview_parts: list[str] = []
        preview_bytes = 0
        with connection:
            for document in rows:
                if row_count >= int(max_rows):
                    raise ResultResourceUnavailableError(
                        "streamed result exceeds the frozen exact-voting safety limit",
                        context={
                            "backend": EXACT_RESULT_FINGERPRINT_BACKEND,
                            "max_rows": int(max_rows),
                            "rows_consumed": row_count + 1,
                            "failure_kind": "result_resource_unavailable",
                        },
                    )
                normalized = _normalize_doc(document)
                key = row_values_key(normalized)
                connection.execute(
                    "INSERT INTO value_counts(value_key, multiplicity) VALUES (?, 1) "
                    "ON CONFLICT(value_key) DO UPDATE SET multiplicity=multiplicity+1",
                    (key,),
                )
                row_count += 1
                if preview_bytes < EXACT_RESULT_PREVIEW_UTF8_BYTES:
                    rendered = canonical_json(normalized)
                    separator = "\n" if preview_parts else ""
                    remaining = EXACT_RESULT_PREVIEW_UTF8_BYTES - preview_bytes
                    chunk = (separator + rendered).encode("utf-8")[:remaining]
                    decoded = chunk.decode("utf-8", errors="ignore")
                    if decoded:
                        preview_parts.append(decoded)
                        preview_bytes += len(decoded.encode("utf-8"))
        digest = _exact_result_digest(connection, row_count)
        connection.close()
        connection = None
        spill_bytes = path.stat().st_size
        result = ExactResultFingerprint(
            path=path,
            row_count=row_count,
            digest=digest,
            preview="".join(preview_parts),
            spill_bytes=spill_bytes,
            stream_source=stream_source,
        )
        completed = True
        return result
    except ExecutionError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise ResultResourceUnavailableError(
            "cannot build exact streamed result fingerprint",
            context={
                "backend": EXACT_RESULT_FINGERPRINT_BACKEND,
                "spill_dir": str(spill_dir) if spill_dir is not None else None,
                "error": str(exc)[:300],
            },
        ) from exc
    finally:
        if connection is not None:
            connection.close()
        if path is not None and not completed:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def run_pipeline_fingerprint(
    world: WorldAccess,
    coll: str,
    pipe: list[dict[str, Any]],
    *,
    timeout_ms: int = 20_000,
    spill_dir: Path | None = None,
) -> ExactResultFingerprint:
    """Execute once and retain only exact disk-backed result-voting evidence."""

    stream = getattr(world, "iter_aggregate", None)
    if callable(stream):
        rows = stream(coll, pipe, max_time_ms=timeout_ms)
        source = "world.iter_aggregate"
    else:
        # Compatibility for small offline test worlds.  Formal/live MongoWorld always
        # exposes ``iter_aggregate`` and the campaign verifier requires that receipt.
        rows = iter(world.aggregate(coll, pipe, max_time_ms=timeout_ms))
        source = "world.aggregate_test_fallback"
    return build_exact_result_fingerprint(
        rows,
        spill_dir=spill_dir,
        stream_source=source,
        max_rows=EXACT_RESULT_MAX_ROWS,
    )


def run_pipeline_preview(
    world: WorldAccess,
    coll: str,
    pipe: list[dict[str, Any]],
    *,
    timeout_ms: int = 20_000,
    max_rows: int = REPAIR_RESULT_PREVIEW_ROWS,
) -> list[dict[str, Any]]:
    """Read only the evidence repair actually consumes: emptiness and first five rows.

    Existing repair logic checks ``not result`` and scans at most five rows for a
    synthetic ``_id``.  Consuming more rows cannot change either decision, so closing the
    cursor after this prefix preserves every model-visible feedback byte while preventing
    ``aggregate([])`` from materializing a whole collection.
    """

    stream = getattr(world, "iter_aggregate", None)
    if not callable(stream):
        return run_pipeline(world, coll, pipe, timeout_ms=timeout_ms)[: int(max_rows)]
    iterator = iter(stream(coll, pipe, max_time_ms=timeout_ms))
    rows: list[dict[str, Any]] = []
    try:
        for _ in range(int(max_rows)):
            try:
                document = next(iterator)
            except StopIteration:
                break
            rows.append(_normalize_doc(document))
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
    return rows


def run_pipeline(
    world: WorldAccess, coll: str, pipe: list[dict[str, Any]], *, timeout_ms: int = 20_000
) -> list[dict[str, Any]]:
    """Execute and normalize (the same normalization evaluation uses)."""
    return [_normalize_doc(d) for d in world.aggregate(coll, pipe, max_time_ms=timeout_ms)]


def stage_count(
    world: WorldAccess, coll: str, prefix: list[dict[str, Any]], *, timeout_ms: int = 15_000
) -> int | str:
    try:
        r = world.aggregate(coll, prefix + [{"$count": "n"}], max_time_ms=timeout_ms)
        return int(r[0]["n"]) if r else 0
    except ExecutionError as exc:
        detail = str(exc.context.get("error") or exc.message)
        return f"ERROR: {detail[:100]}"


def match_paths_values(stage: dict[str, Any]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    m = stage.get("$match")
    if isinstance(m, dict):
        for k, v in m.items():
            if k.startswith("$"):
                continue
            if isinstance(v, dict):
                for op in ("$eq", "$in"):
                    if op in v:
                        out.append((k, v[op]))
            else:
                out.append((k, v))
    return out


def distinct_sample(
    world: WorldAccess, coll: str, path: str, k: int = 8, *, timeout_ms: int = 8_000
) -> list[Any] | None:
    try:
        r = world.aggregate(
            coll, [{"$group": {"_id": f"${path}"}}, {"$limit": k}], max_time_ms=timeout_ms
        )
        return [x["_id"] for x in r]
    except ExecutionError:
        return None


def bisect_empty(
    world: WorldAccess,
    index: "GroundingIndex | None",
    coll: str,
    pipe: list[dict[str, Any]],
    *,
    stage_timeout_ms: int = 15_000,
    distinct_k: int = 8,
) -> str | None:
    """Locate the stage where the row count first drops to 0 / errors; describe it
    with actual data values at the filtered paths."""
    prev: int | None = None
    for k in range(1, len(pipe) + 1):
        n = stage_count(world, coll, pipe[:k], timeout_ms=stage_timeout_ms)
        if isinstance(n, str):
            return f"stage {k} ({json.dumps(pipe[k - 1])[:_STAGE_PREVIEW_CHARS]}) raises {n}"
        if n == 0:
            stage = pipe[k - 1]
            msg = (
                f"stage {k} ({json.dumps(stage)[:_STAGE_PREVIEW_CHARS]}) reduces "
                f"{prev if prev is not None else 'all'} rows -> 0."
            )
            dyn = index.dynamic_maps.get(coll, {}) if index is not None else {}
            for path, _val in match_paths_values(stage)[:2]:
                if index is not None and dyn_prefixed(index, coll, path):
                    dp = next(
                        d
                        for d in dyn
                        if path == d or path.startswith(d + ".") or d.startswith(path)
                    )
                    msg += (
                        f" `{path}` is under dynamic-key map `{dp}` "
                        f"(example keys {list(dyn[dp])[:4]})."
                    )
                else:
                    vals = distinct_sample(world, coll, path, distinct_k)
                    if vals is not None:
                        msg += (
                            f" Actual values at `{path}` (sample): "
                            f"{json.dumps(vals, default=str)[:_DISTINCT_PREVIEW_CHARS]}."
                        )
            return msg
        prev = n
    return None


def synthetic_id_violation(rows: list[dict[str, Any]]) -> str | None:
    """Result rows leak the release-world synthetic document key ('db::42')."""
    if any(
        isinstance(d, dict) and isinstance(d.get("_id"), str) and "::" in d["_id"]
        for d in rows[:_SYNTHETIC_ID_SCAN_ROWS]
    ):
        return (
            "result rows carry the internal document `_id` "
            "(synthetic key like 'db::42'); the question does not "
            "ask for it — exclude it with `_id: 0` in the final "
            "$project."
        )
    return None
