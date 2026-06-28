"""Flask demonstration surface for the TEND SAG solver.

The demo is intentionally a thin adapter over the real release dataset and
solver runtime. It does not carry its own copied MongoDB JSON payloads.
"""
from __future__ import annotations

import asyncio
import atexit
from collections import Counter
from concurrent.futures import TimeoutError as FutureTimeoutError
import json
import os
import re
import sys
import time
from functools import lru_cache
from pathlib import Path
import threading
from typing import Any

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tend.cli import build_solver_runtime  # noqa: E402
from tend.config import Settings  # noqa: E402
from tend.errors import TendError  # noqa: E402
from tend.release_layout import resolve_release_dataset_layout  # noqa: E402
from tend.solver.sag import GroundingIndexCache, SAGPolicy, sag_solve_record  # noqa: E402


DEFAULT_DATASET_DIR = REPO_ROOT / "release" / "tend-native-mongodb-v1"
_RAW_DEFAULT_SOLVER_MODE = os.environ.get("TEND_DEMO_SOLVER_MODE", "stub").strip().lower()
DEFAULT_SOLVER_MODE = _RAW_DEFAULT_SOLVER_MODE if _RAW_DEFAULT_SOLVER_MODE in {"stub", "live"} else "stub"
MAX_EXAMPLES = 16
MAX_SAMPLE_DOCS_PER_COLLECTION = 2
MAX_FIELD_SHAPE_DOCS_PER_COLLECTION = 48
MAX_SHAPE_CHILDREN = 12
MAX_DYNAMIC_MAPS_PER_COLLECTION = 10
MAX_DYNAMIC_VALUE_FIELDS = 12
MAX_DYNAMIC_KEY_SAMPLES = 8
MAX_SHAPE_DEPTH = 5
MAX_EXECUTION_ROWS = 50
SOLVE_TIMEOUT_S = max(1.0, float(os.environ.get("TEND_DEMO_SOLVE_TIMEOUT_S", "90")))
POLICY_LIMITS = {
    "k_consistency": (1, 3),
    "max_repair_rounds": (1, 6),
    "sample_docs": (1, 400),
    "card_cap": (1, 400),
}
CARD_MODES = {"lattice", "toplevel", "nocollapse"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DATA_LIKE_KEY_RE = re.compile(
    r"(^\d{4}(-\d{4})?$)|(::)|(^[A-Z]$)|(^[PK](-|\b))|(^\d+(-\d+)?$)|(_grade_span$)"
)

app = Flask(__name__)
app.secret_key = os.environ.get("TEND_DEMO_SECRET_KEY", "tend-demo-local-only")


class DemoError(Exception):
    """Expected user-facing demo error."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class _RuntimeBundle:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.settings = _settings_for_mode(mode)
        self.runtime = build_solver_runtime(self.settings, run_kind="demo_solver")
        self.index_cache = GroundingIndexCache(
            self.runtime.mongo,
            self.runtime.settings,
            self.runtime.log,
        )
        self.loaded_witness_dbs: set[str] = set()
        self._witness_locks: dict[str, asyncio.Lock] = {}

    async def ensure_witness_loaded(
        self,
        db_id: str,
        local_data: dict[str, list[dict[str, Any]]],
    ) -> bool:
        """Load one witness DB once per runtime bundle without blocking the loop."""
        if self.runtime.settings.use_existing_mongo_dbs:
            return True
        if db_id in self.loaded_witness_dbs:
            return True

        lock = self._witness_locks.get(db_id)
        if lock is None:
            lock = asyncio.Lock()
            self._witness_locks[db_id] = lock

        async with lock:
            if db_id in self.loaded_witness_dbs:
                return True
            if not await asyncio.to_thread(self.runtime.mongo.available):
                return False
            await asyncio.to_thread(self.runtime.mongo.load_witness, db_id, local_data)
            self.loaded_witness_dbs.add(db_id)
            return True

    async def close(self) -> None:
        errors: list[str] = []
        if self.runtime.source is not None:
            try:
                self.runtime.source.close()
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                errors.append(f"source: {type(exc).__name__}: {exc}")
        try:
            self.runtime.mongo.close()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            errors.append(f"mongo: {type(exc).__name__}: {exc}")
        try:
            try:
                await self.runtime.ctx.llm.aclose()
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                errors.append(f"llm: {type(exc).__name__}: {exc}")
        finally:
            try:
                self.runtime.log.close()
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                errors.append(f"log: {type(exc).__name__}: {exc}")
        if errors:
            app.logger.warning("Demo runtime cleanup had errors: %s", "; ".join(errors))


class DemoSolverService:
    """Own one async solver loop and reusable runtime/cache per mode."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._bundles: dict[str, _RuntimeBundle] = {}

    def solve(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_loop()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(_solve_with_solver(payload), self._loop)
        try:
            return future.result(timeout=SOLVE_TIMEOUT_S)
        except FutureTimeoutError as exc:
            future.cancel()
            raise DemoError(
                f"Solver timed out after {SOLVE_TIMEOUT_S:g} seconds.",
                status_code=504,
            ) from exc

    def runtime_for_mode(self, mode: str) -> _RuntimeBundle:
        bundle = self._bundles.get(mode)
        if bundle is None:
            bundle = _RuntimeBundle(mode)
            self._bundles[mode] = bundle
        return bundle

    def shutdown(self) -> None:
        loop = self._loop
        if loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._close_bundles(), loop)
        try:
            future.result(timeout=15)
        except Exception as exc:  # noqa: BLE001 - shutdown must continue
            app.logger.warning("Demo runtime shutdown cleanup failed: %s", exc)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=15)
            self._loop = None
            self._thread = None

    async def _close_bundles(self) -> None:
        bundles = list(self._bundles.values())
        self._bundles.clear()
        for bundle in bundles:
            await bundle.close()

    def _ensure_loop(self) -> None:
        if self._loop is not None:
            return
        with self._lock:
            if self._loop is not None:
                return
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._thread = threading.Thread(
                target=self._run_loop,
                args=(loop,),
                name="tend-demo-solver-loop",
                daemon=True,
            )
            self._thread.start()

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


def _json_success(**payload: Any):
    return jsonify({"status": "success", **payload})


def _json_error(message: str, *, status_code: int = 400, **payload: Any):
    return jsonify({"status": "error", "message": message, **payload}), status_code


def _dataset_label(path: Path) -> str:
    return path.name or str(path)


def _dataset_dir() -> Path:
    raw = os.environ.get("TEND_DEMO_DATASET_DIR")
    path = Path(raw).expanduser() if raw else DEFAULT_DATASET_DIR
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    return path


def _layout():
    dataset_dir = _dataset_dir()
    layout = resolve_release_dataset_layout(dataset_dir)
    if not layout.test_path.exists():
        raise DemoError(
            f"Release dataset not found at {dataset_dir}. "
            "Set TEND_DEMO_DATASET_DIR to a valid TEND release directory.",
            status_code=500,
        )
    return layout


@lru_cache(maxsize=4)
def _records_for_dataset(dataset_dir: str) -> list[dict[str, Any]]:
    layout = resolve_release_dataset_layout(Path(dataset_dir))
    return json.loads(layout.test_path.read_text(encoding="utf-8"))


def _records() -> list[dict[str, Any]]:
    return _records_for_dataset(str(_dataset_dir()))


@lru_cache(maxsize=64)
def _load_schema(dataset_dir: str, db_id: str) -> dict[str, Any]:
    layout = resolve_release_dataset_layout(Path(dataset_dir))
    path = layout.mongodb_schema_dir / f"{db_id}.json"
    if not path.exists():
        raise DemoError(f"No schema found for database {db_id!r}", status_code=404)
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=3)
def _load_data(dataset_dir: str, db_id: str) -> dict[str, list[dict[str, Any]]]:
    layout = resolve_release_dataset_layout(Path(dataset_dir))
    path = layout.mongodb_data_dir / f"{db_id}.json"
    if not path.exists():
        raise DemoError(f"No MongoDB witness data found for database {db_id!r}", status_code=404)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DemoError(f"MongoDB witness data for {db_id!r} is not an object", status_code=500)
    out: dict[str, list[dict[str, Any]]] = {}
    invalid_collections: list[str] = []
    invalid_documents: dict[str, int] = {}
    for collection, docs in data.items():
        collection_name = str(collection)
        if not isinstance(docs, list):
            invalid_collections.append(collection_name)
            continue
        bad_count = sum(1 for doc in docs if not isinstance(doc, dict))
        if bad_count:
            invalid_documents[collection_name] = bad_count
            continue
        out[collection_name] = docs
    if invalid_collections or invalid_documents:
        raise DemoError(
            f"MongoDB witness data for {db_id!r} is malformed",
            status_code=500,
        )
    return out


def _db_ids() -> list[str]:
    layout = _layout()
    from_schema = {path.stem for path in layout.mongodb_schema_dir.glob("*.json")}
    from_records = {str(row.get("db_id")) for row in _records() if row.get("db_id")}
    return sorted(from_schema & from_records)


def _record_counts_by_db() -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in _records():
        db_id = str(record.get("db_id") or "")
        if db_id:
            counts[db_id] = counts.get(db_id, 0) + 1
    return counts


def _database_summary(db_id: str) -> dict[str, Any]:
    schema = _load_schema(str(_dataset_dir()), db_id)
    collections = schema.get("collections") if isinstance(schema, dict) else {}
    return {
        "db_id": db_id,
        "record_count": _record_counts_by_db().get(db_id, 0),
        "collection_count": len(collections) if isinstance(collections, dict) else 0,
        "source_tables": schema.get("source_tables", []) if isinstance(schema, dict) else [],
    }


def _examples_for_db(db_id: str) -> list[dict[str, Any]]:
    out = []
    for record in _records():
        if record.get("db_id") != db_id:
            continue
        out.append(
            {
                "record_id": record.get("record_id"),
                "db_id": db_id,
                "NLQ": record.get("NLQ", ""),
                "NLQ_colloquial": record.get("NLQ_colloquial", ""),
            }
        )
        if len(out) >= MAX_EXAMPLES:
            break
    return out


def _selected_record(db_id: str, record_id: Any | None, nlq: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "db_id": db_id,
        "nl_queries": {"canonical": nlq},
    }
    if record_id not in (None, ""):
        try:
            parsed_record_id = int(record_id)
        except (TypeError, ValueError):
            parsed_record_id = record_id
        record["record_id"] = parsed_record_id
    return record


def _schema_payload(db_id: str) -> dict[str, Any]:
    dataset_dir = str(_dataset_dir())
    schema = _load_schema(dataset_dir, db_id)
    data = _load_data(dataset_dir, db_id)
    collections = schema.get("collections", {}) if isinstance(schema, dict) else {}
    collection_payload = []
    for name in sorted(data):
        meta = collections.get(name, {}) if isinstance(collections, dict) else {}
        docs = data.get(name, [])
        sampled_docs = docs[:MAX_FIELD_SHAPE_DOCS_PER_COLLECTION]
        inferred_profile = _collection_profile(sampled_docs)
        collection_payload.append(
            {
                "name": name,
                "document_count": len(docs),
                "declared_document_count": meta.get("document_count"),
                "root_entity": meta.get("root_entity"),
                "source_tables": meta.get("source_tables", []),
                "sampled_shape_document_count": len(sampled_docs),
                "field_paths": _field_paths(sampled_docs),
                **inferred_profile,
                "sample_documents": [_compact_value(doc) for doc in docs[:MAX_SAMPLE_DOCS_PER_COLLECTION]],
            }
        )
    structure_audit = schema.get("structure_audit", {}) if isinstance(schema, dict) else {}
    return {
        "db_id": db_id,
        "dataset_dir": _dataset_label(_dataset_dir()),
        "source_tables": schema.get("source_tables", []) if isinstance(schema, dict) else [],
        "collections": collection_payload,
        "dynamic_key_paths": structure_audit.get("dynamic_key_paths", [])[:24]
        if isinstance(structure_audit, dict)
        else [],
    }


def _collection_profile(docs: list[dict[str, Any]]) -> dict[str, Any]:
    root = _shape_node((), docs, parent_count=len(docs), depth=0)
    top_level_fields = [
        _shape_summary(child, denominator=max(len(docs), 1))
        for child in root.get("children", [])
    ]
    dynamic_maps: list[dict[str, Any]] = []
    _collect_dynamic_maps(root, dynamic_maps)
    return {
        "document_shape": root,
        "top_level_fields": top_level_fields,
        "dynamic_maps": dynamic_maps[:MAX_DYNAMIC_MAPS_PER_COLLECTION],
    }


def _shape_node(
    path: tuple[str, ...],
    values: list[Any],
    *,
    parent_count: int,
    depth: int,
) -> dict[str, Any]:
    type_counts = Counter(_value_type(value) for value in values)
    types = sorted(type_counts)
    node: dict[str, Any] = {
        "name": path[-1] if path else "$",
        "path": _display_path(path),
        "kind": _shape_kind(types),
        "types": types,
        "presence_count": len(values),
        "presence_pct": round(100 * len(values) / max(parent_count, 1), 1),
    }
    if depth >= MAX_SHAPE_DEPTH:
        return node

    dict_values = [value for value in values if isinstance(value, dict)]
    list_values = [value for value in values if isinstance(value, list)]

    if dict_values:
        key_counts: Counter[str] = Counter()
        values_by_key: dict[str, list[Any]] = {}
        for value in dict_values:
            for key, child in value.items():
                key_name = str(key)
                key_counts[key_name] += 1
                values_by_key.setdefault(key_name, []).append(child)

        keys = sorted(key_counts)
        if path and _is_dynamic_map_path(path, keys):
            placeholder = _dynamic_placeholder(path, keys)
            child_values = [
                child
                for value in dict_values
                for child in value.values()
            ]
            node.update(
                {
                    "kind": "dynamic_map",
                    "key_count": len(keys),
                    "key_samples": keys[:MAX_DYNAMIC_KEY_SAMPLES],
                    "placeholder": placeholder,
                    "value_path": _display_path(path + (placeholder,)),
                }
            )
            if child_values:
                node["children"] = [
                    _shape_node(
                        path + (placeholder,),
                        child_values,
                        parent_count=len(child_values),
                        depth=depth + 1,
                    )
                ]
            return node

        children = []
        for key, _count in sorted(key_counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_SHAPE_CHILDREN]:
            children.append(
                _shape_node(
                    path + (key,),
                    values_by_key[key],
                    parent_count=len(dict_values),
                    depth=depth + 1,
                )
            )
        if children:
            node["kind"] = "object"
            node["children"] = children
        if len(key_counts) > MAX_SHAPE_CHILDREN:
            node["truncated_children"] = len(key_counts) - MAX_SHAPE_CHILDREN

    if list_values:
        lengths = [len(value) for value in list_values]
        node["kind"] = "array" if not dict_values else "mixed"
        node["array"] = {
            "min_length": min(lengths) if lengths else 0,
            "max_length": max(lengths) if lengths else 0,
            "avg_length": round(sum(lengths) / len(lengths), 1) if lengths else 0,
        }
        item_values = [
            item
            for value in list_values
            for item in value[:8]
        ][:240]
        if item_values:
            node.setdefault("children", [])
            node["children"].append(
                _shape_node(
                    path + ("[]",),
                    item_values,
                    parent_count=max(sum(lengths), 1),
                    depth=depth + 1,
                )
            )

    return node


def _collect_dynamic_maps(node: dict[str, Any], out: list[dict[str, Any]]) -> None:
    if node.get("kind") == "dynamic_map":
        value_node = next(iter(node.get("children", [])), {})
        out.append(
            {
                "path": node.get("path"),
                "value_path": node.get("value_path"),
                "placeholder": node.get("placeholder"),
                "key_count": node.get("key_count", 0),
                "key_samples": node.get("key_samples", []),
                "value_types": value_node.get("types", []),
                "value_kind": value_node.get("kind", "unknown"),
                "value_fields": _dynamic_value_fields(value_node),
            }
        )
    for child in node.get("children", []):
        _collect_dynamic_maps(child, out)


def _dynamic_value_fields(value_node: dict[str, Any]) -> list[dict[str, Any]]:
    children = value_node.get("children", [])
    if not isinstance(children, list):
        return []
    fields = []
    for child in children[:MAX_DYNAMIC_VALUE_FIELDS]:
        fields.append(_shape_summary(child, denominator=max(value_node.get("presence_count", 1), 1)))
    return fields


def _shape_summary(node: dict[str, Any], *, denominator: int) -> dict[str, Any]:
    children = node.get("children", [])
    child_count = len(children) if isinstance(children, list) else 0
    return {
        "name": node.get("name"),
        "path": node.get("path"),
        "kind": node.get("kind"),
        "types": node.get("types", []),
        "presence_count": node.get("presence_count", 0),
        "presence_pct": round(100 * int(node.get("presence_count", 0)) / max(denominator, 1), 1),
        "child_count": child_count,
        "key_count": node.get("key_count"),
        "key_samples": node.get("key_samples", []),
        "array": node.get("array"),
    }


def _display_path(path: tuple[str, ...]) -> str:
    if not path:
        return "$"
    out = ""
    for part in path:
        if part == "[]":
            out += "[]"
        elif part.startswith("{") and part.endswith("}"):
            out += f".{part}" if out else part
        else:
            out += f".{part}" if out else part
    return out


def _shape_kind(types: list[str]) -> str:
    if not types:
        return "unknown"
    if len(types) > 1:
        composite = [type_name for type_name in types if type_name in {"object", "array"}]
        return composite[0] if len(types) == 2 and "null" in types and composite else "mixed"
    only = types[0]
    if only in {"object", "array"}:
        return only
    return "scalar"


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_dynamic_map_path(path: tuple[str, ...], keys: list[str]) -> bool:
    if not keys:
        return False
    field_name = path[-1]
    if "_by_" in field_name:
        return True
    if len(keys) > MAX_SHAPE_CHILDREN * 2:
        return True
    data_like_keys = sum(1 for key in keys if DATA_LIKE_KEY_RE.search(key))
    return len(keys) > MAX_SHAPE_CHILDREN and data_like_keys >= min(4, len(keys))


def _dynamic_placeholder(path: tuple[str, ...], keys: list[str]) -> str:
    field_name = path[-1]
    if "_by_" in field_name:
        suffix = field_name.split("_by_", 1)[1].strip("_") or "key"
        return "{" + suffix + "}"
    if any("::" in key for key in keys):
        return "{compound_key}"
    if any(re.fullmatch(r"\d{4}(-\d{4})?", key) for key in keys):
        return "{year}"
    return "{key}"


def _field_paths(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    paths: dict[str, set[str]] = {}
    for doc in docs:
        _walk_shape(doc, (), paths)
    return [
        {"path": path, "types": sorted(types)}
        for path, types in sorted(paths.items())
        if path
    ][:220]


def _walk_shape(value: Any, path: tuple[str, ...], paths: dict[str, set[str]]) -> None:
    if isinstance(value, dict):
        if path:
            paths.setdefault(".".join(path), set()).add("object")
        for key, child in value.items():
            _walk_shape(child, path + (str(key),), paths)
        return
    if isinstance(value, list):
        if path:
            paths.setdefault(".".join(path), set()).add("array")
        for item in value[:3]:
            _walk_shape(item, path + ("[]",), paths)
        return
    if path:
        paths.setdefault(".".join(path), set()).add(type(value).__name__ if value is not None else "null")


def _compact_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        if isinstance(value, dict):
            return {"__truncated_object__": sorted(str(key) for key in value)[:16]}
        if isinstance(value, list):
            return {"__truncated_array__": len(value)}
    if isinstance(value, dict):
        items = list(value.items())
        out = {str(key): _compact_value(child, depth=depth + 1) for key, child in items[:18]}
        if len(items) > 18:
            out["__truncated_keys__"] = len(items) - 18
        return out
    if isinstance(value, list):
        out = [_compact_value(item, depth=depth + 1) for item in value[:6]]
        if len(value) > 6:
            out.append({"__truncated_items__": len(value) - 6})
        return out
    if isinstance(value, str) and len(value) > 180:
        return f"{value[:177]}..."
    return value


def _solver_mode(raw: Any) -> str:
    mode = str(raw or DEFAULT_SOLVER_MODE or "stub").strip().lower()
    if mode not in {"stub", "live"}:
        raise DemoError("solver mode must be 'stub' or 'live'")
    return mode


def _parse_bool(value: Any, *, default: bool, field: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    raise DemoError(f"{field} must be a boolean")


def _bounded_policy_int(options: dict[str, Any], key: str, default: int) -> int:
    value = options.get(key, default)
    low, high = POLICY_LIMITS[key]
    if isinstance(value, bool):
        raise DemoError(f"{key} must be an integer between {low} and {high}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DemoError(f"{key} must be an integer between {low} and {high}") from exc
    if parsed < low or parsed > high:
        raise DemoError(f"{key} must be between {low} and {high}")
    return parsed


def _solver_policy(payload: dict[str, Any]) -> SAGPolicy:
    fast = _parse_bool(payload.get("fastMode"), default=True, field="fastMode")
    raw_options = payload.get("solverOptions")
    if raw_options not in (None, "") and not isinstance(raw_options, dict):
        raise DemoError("solverOptions must be a JSON object")
    options = raw_options if isinstance(raw_options, dict) else {}
    allowed_options = set(POLICY_LIMITS) | {"card_mode"}
    unknown_options = sorted(str(key) for key in options if key not in allowed_options)
    if unknown_options:
        raise DemoError(f"Unsupported solver option(s): {', '.join(unknown_options)}")
    defaults = {
        "k_consistency": 1 if fast else 3,
        "max_repair_rounds": 1 if fast else 6,
        "sample_docs": 80 if fast else 400,
        "card_cap": 260 if fast else 400,
        "card_mode": "lattice",
    }
    defaults.update({key: value for key, value in options.items() if value not in (None, "")})
    card_mode = str(defaults["card_mode"])
    if card_mode not in CARD_MODES:
        raise DemoError(f"card_mode must be one of: {', '.join(sorted(CARD_MODES))}")
    policy = SAGPolicy(
        k_consistency=_bounded_policy_int(defaults, "k_consistency", 1),
        max_repair_rounds=_bounded_policy_int(defaults, "max_repair_rounds", 1),
        sample_docs=_bounded_policy_int(defaults, "sample_docs", 80),
        card_cap=_bounded_policy_int(defaults, "card_cap", 260),
        card_mode=card_mode,
    )
    policy.validate()
    return policy


def _settings_for_mode(mode: str) -> Settings:
    overrides = {
        "TEND_QUIET": "1",
        "TEND_LLM_TRANSCRIPT_MD": os.environ.get("TEND_DEMO_TRANSCRIPT_MD", "0"),
        "TEND_MAX_RETRIES": os.environ.get("TEND_DEMO_MAX_RETRIES", "0"),
        "TEND_LLM_MAX_CONCURRENCY": "1",
    }
    if mode == "stub":
        overrides["TEND_LLM_STUB"] = "1"
    else:
        overrides["TEND_LLM_STUB"] = "0"
    settings = Settings.from_env(
        overrides=overrides,
        require_bird=False,
        require_llm=mode == "live",
    )
    if mode == "live" and settings.stub:
        raise DemoError("Live solver mode cannot run with TEND_LLM_STUB enabled.", status_code=500)
    return settings


async def _solve_with_solver(payload: dict[str, Any]) -> dict[str, Any]:
    db_id = str(payload.get("database") or payload.get("db_id") or "").strip()
    nlq = str(payload.get("query") or payload.get("nlq") or "").strip()
    if not db_id:
        raise DemoError("Database is required")
    if db_id not in _db_ids():
        raise DemoError(f"Unknown database {db_id!r}", status_code=404)
    if not nlq:
        raise DemoError("Natural language query is required")

    mode = _solver_mode(payload.get("mode"))
    policy = _solver_policy(payload)
    record = _selected_record(db_id, payload.get("record_id"), nlq)
    schema = _load_schema(str(_dataset_dir()), db_id)
    local_data = _load_data(str(_dataset_dir()), db_id)
    bundle = SOLVER_SERVICE.runtime_for_mode(mode)
    rt = bundle.runtime
    started = time.monotonic()
    witness_preloaded = False
    if local_data and not rt.settings.stub:
        witness_preloaded = await bundle.ensure_witness_loaded(db_id, local_data)
    result = await sag_solve_record(
        rt.workflow,
        record,
        schema,
        local_data=local_data,
        policy=policy,
        index_cache=bundle.index_cache,
        witness_preloaded=witness_preloaded,
        stage="demo_solver",
    )
    result_payload = result.to_json()
    execution = None
    if _parse_bool(payload.get("execute"), default=False, field="execute") and result_payload.get("result_type") == "solver_prediction":
        execution = await _execute_prediction(
            bundle,
            db_id,
            str(result_payload.get("MQL") or ""),
            local_data,
        )
    return {
        "mode": mode,
        "elapsed_s": round(time.monotonic() - started, 3),
        "policy": {
            "k_consistency": policy.k_consistency,
            "max_repair_rounds": policy.max_repair_rounds,
            "sample_docs": policy.sample_docs,
            "card_cap": policy.card_cap,
            "card_mode": policy.card_mode,
            "solver_variant": policy.solver_variant,
        },
        "result": result_payload,
        "execution": execution,
        "run_id": rt.settings.run_id,
        "run_dir_name": Path(rt.settings.run_dir).name,
    }


async def _execute_prediction(
    bundle: _RuntimeBundle,
    db_id: str,
    mql: str,
    local_data: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    rt = bundle.runtime
    if not mql:
        return {"status": "skipped", "reason": "solver did not produce MQL"}
    if not await asyncio.to_thread(rt.mongo.available):
        return {"status": "skipped", "reason": "MongoDB is unavailable"}
    try:
        if not rt.settings.use_existing_mongo_dbs:
            loaded = await bundle.ensure_witness_loaded(db_id, local_data)
            if not loaded:
                return {"status": "skipped", "reason": "MongoDB is unavailable"}
        probe = await asyncio.to_thread(rt.mongo.run_readonly_probe, db_id, mql, limit=MAX_EXECUTION_ROWS)
    except Exception as exc:  # noqa: BLE001 - returned to the demo UI
        app.logger.exception("Demo read-only execution failed")
        return {
            "status": "error",
            "message": "Read-only execution failed.",
            "error_type": type(exc).__name__,
        }
    return {
        "status": "success",
        "collection": probe.get("collection"),
        "stage_count": probe.get("stage_count"),
        "probe": probe,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/api/health")
def health():
    layout = _layout()
    return _json_success(
        dataset_dir=_dataset_label(layout.root),
        record_count=len(_records()),
        database_count=len(_db_ids()),
        default_mode=DEFAULT_SOLVER_MODE,
    )


@app.route("/api/databases")
def databases():
    layout = _layout()
    return _json_success(
        dataset_dir=_dataset_label(layout.root),
        databases=[_database_summary(db_id) for db_id in _db_ids()],
        default_mode=DEFAULT_SOLVER_MODE,
    )


@app.route("/api/examples/<db_id>")
def examples(db_id: str):
    if db_id not in _db_ids():
        raise DemoError(f"Unknown database {db_id!r}", status_code=404)
    return _json_success(examples=_examples_for_db(db_id))


@app.route("/api/schema/<db_id>")
def schema(db_id: str):
    if db_id not in _db_ids():
        raise DemoError(f"Unknown database {db_id!r}", status_code=404)
    return _json_success(schema=_schema_payload(db_id))


@app.route("/api/solve", methods=["POST"])
def solve():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise DemoError("Request body must be a JSON object")
    return _json_success(**SOLVER_SERVICE.solve(payload))


# Backward-compatible endpoints for older bookmarks/scripts.
@app.route("/get_databases")
def legacy_databases():
    return databases()


@app.route("/get_schema/<db_name>")
def legacy_schema(db_name: str):
    return schema(db_name)


@app.route("/query", methods=["POST"])
def legacy_query():
    raw_payload = request.get_json(silent=True)
    if not isinstance(raw_payload, dict):
        raise DemoError("Request body must be a JSON object")
    payload = dict(raw_payload)
    payload["execute"] = not _parse_bool(
        payload.get("generateOnly"),
        default=False,
        field="generateOnly",
    )
    response = SOLVER_SERVICE.solve(payload)
    result = response.get("result", {})
    execution = response.get("execution") or {}
    return _json_success(
        mongo_query=result.get("MQL", ""),
        results=execution.get("probe"),
        execution=execution,
        solver=response,
    )


@app.errorhandler(DemoError)
def handle_demo_error(exc: DemoError):
    return _json_error(exc.message, status_code=exc.status_code)


@app.errorhandler(HTTPException)
def handle_http_error(exc: HTTPException):
    return _json_error(exc.description, status_code=exc.code or 500)


@app.errorhandler(TendError)
def handle_tend_error(exc: TendError):
    app.logger.warning("TEND demo error: %s", exc.message)
    return _json_error(exc.message, status_code=500)


@app.errorhandler(Exception)
def handle_unexpected_error(exc: Exception):
    app.logger.exception("Unexpected demo server error")
    return _json_error("Unexpected demo server error.", status_code=500)


SOLVER_SERVICE = DemoSolverService()
atexit.register(SOLVER_SERVICE.shutdown)


if __name__ == "__main__":
    port = int(os.environ.get("TEND_DEMO_PORT", "5000"))
    host = os.environ.get("TEND_DEMO_HOST", "127.0.0.1")
    debug = _parse_bool(os.environ.get("TEND_DEMO_DEBUG"), default=False, field="TEND_DEMO_DEBUG")
    if debug and host not in LOOPBACK_HOSTS:
        raise SystemExit("Refusing to enable Flask debug mode on a non-loopback host.")
    app.run(host=host, port=port, debug=debug)
