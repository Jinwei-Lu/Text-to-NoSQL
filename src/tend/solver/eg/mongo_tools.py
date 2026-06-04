"""MongoDB-backed SMART-EG environment tools."""
from __future__ import annotations

from collections import Counter
import json
from typing import Any, Mapping

from ...execution.ast_check import scan_disabled

from .execution import render_mql
from .safety import (
    DEFAULT_DOC_LIMIT,
    DEFAULT_VALUE_LIMIT,
    MAX_DOC_LIMIT,
    MAX_LITERAL_VALUE_BUCKETS,
    MAX_VALUE_LIMIT,
    bounded_limit,
    extract_path_values,
    flatten_extracted_values,
    redact_value,
    summarize_path_map,
    summarize_redacted_value,
    summarize_type_counts,
    stable_hash,
    value_kind,
    walk_document_paths,
)

MAX_SAMPLE_PATHS = 40
MAX_SAMPLE_KIND_PATHS = 24
MAX_DYNAMIC_KEY_PATHS = 24
MAX_DYNAMIC_KEY_SAMPLES = 8


class SmartEgMongoTools:
    """Bounded read-only Mongo observations for SMART-EG."""

    def __init__(self, mongo: Any, db_id: str) -> None:
        self.mongo = mongo
        self.db_id = db_id

    def list_collections(self, _request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        collections = self.mongo.list_collections(self.db_id)
        clean: list[Any] = []
        for item in collections:
            if isinstance(item, dict) and item.get("collection"):
                clean.append(
                    {
                        "collection": str(item.get("collection", "")),
                        "estimated_document_count": int(item.get("estimated_document_count", 0)),
                    }
                )
            elif isinstance(item, str) and item:
                clean.append(item)
        return {
            "tool": "list_collections",
            "db_id": self.db_id,
            "collection_count": len(clean),
            "collections": clean,
        }

    def sample_documents(
        self,
        collection: str | Mapping[str, Any],
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        request = collection if isinstance(collection, Mapping) else {}
        collection_name = str(request.get("collection") if request else collection)
        sample_limit = bounded_limit(
            request.get("limit", limit) if request else limit,
            default=5,
            maximum=MAX_DOC_LIMIT,
        )
        docs = self._sample(collection, sample_limit)
        path_values: dict[str, list[Any]] = {}
        top_level_values: dict[str, list[Any]] = {}
        for doc in docs:
            for key, value in doc.items():
                top_level_values.setdefault(str(key), []).append(value)
            for path, values in walk_document_paths(doc).items():
                path_values.setdefault(path, []).extend(values)
        return {
            "tool": "sample_documents",
            "db_id": self.db_id,
            "collection": collection_name,
            "limit": sample_limit,
            "sample_count": len(docs),
            "path_count": len(path_values),
            "top_level_keys": sorted(top_level_values)[:MAX_SAMPLE_PATHS],
            "top_level_type_counts": {
                key: summarize_type_counts(values)
                for key, values in sorted(top_level_values.items())[:MAX_SAMPLE_PATHS]
            },
            "paths": _bounded_path_summary(path_values, limit=MAX_SAMPLE_PATHS),
            "array_paths": _path_kind_samples(path_values, "array", limit=MAX_SAMPLE_KIND_PATHS),
            "object_paths": _path_kind_samples(path_values, "object", limit=MAX_SAMPLE_KIND_PATHS),
            "dynamic_key_candidates": _dynamic_key_candidates(docs),
            "redaction": {
                "raw_rows": False,
                "raw_documents": False,
                "scalar_values": "hash_only",
                "dynamic_keys": "hash_only",
            },
        }

    def discover_paths(self, collection: str, *, limit: int | None = None) -> dict[str, Any]:
        docs = self._sample(collection, bounded_limit(limit, default=DEFAULT_DOC_LIMIT,
                                                      maximum=MAX_DOC_LIMIT))
        path_values: dict[str, list[Any]] = {}
        for doc in docs:
            for path, values in walk_document_paths(doc).items():
                path_values.setdefault(path, []).extend(values)
        paths = _bounded_path_summary(path_values, limit=MAX_SAMPLE_PATHS)
        return {
            "tool": "discover_paths",
            "db_id": self.db_id,
            "collection": collection,
            "document_count": len(docs),
            "path_count": len(path_values),
            "returned_path_count": len(paths),
            "omitted_path_count": max(0, len(path_values) - len(paths)),
            "paths": paths,
            "redaction": {"raw_rows": False},
        }

    def profile_path(
        self,
        collection: str | Mapping[str, Any],
        path: str | None = None,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        request = collection if isinstance(collection, Mapping) else {}
        collection_name = str(request.get("collection") if request else collection)
        target_path = str(request.get("path") if request else path)
        docs = self._sample(
            collection_name,
            bounded_limit(
                request.get("limit", limit) if request else limit,
                default=DEFAULT_DOC_LIMIT,
                maximum=MAX_DOC_LIMIT,
            ),
        )
        present_count = 0
        values: list[Any] = []
        for doc in docs:
            extracted = flatten_extracted_values(extract_path_values(doc, target_path))
            if extracted:
                present_count += 1
                values.extend(extracted)
        return {
            "tool": "profile_path",
            "db_id": self.db_id,
            "collection": collection_name,
            "path": target_path,
            "document_count": len(docs),
            "present_count": present_count,
            "exists_count": present_count,
            "missing_count": len(docs) - present_count,
            "value_count": len(values),
            "type_counts": summarize_type_counts(values),
            "redaction": {"raw_rows": False},
        }

    def profile_path_values(
        self,
        collection: str | Mapping[str, Any],
        path: str | None = None,
        *,
        limit: int | None = None,
        value_limit: int | None = None,
    ) -> dict[str, Any]:
        request = collection if isinstance(collection, Mapping) else {}
        collection_name = str(request.get("collection") if request else collection)
        target_path = str(request.get("path") if request else path)
        docs = self._sample(
            collection_name,
            bounded_limit(
                request.get("limit", limit) if request else limit,
                default=DEFAULT_DOC_LIMIT,
                maximum=MAX_DOC_LIMIT,
            ),
        )
        max_values = bounded_limit(
            request.get("value_limit", value_limit) if request else value_limit,
            default=DEFAULT_VALUE_LIMIT,
            maximum=MAX_VALUE_LIMIT,
        )
        values: list[Any] = []
        for doc in docs:
            values.extend(flatten_extracted_values(extract_path_values(doc, target_path)))
        buckets: dict[str, dict[str, Any]] = {}
        for value in values:
            key = stable_hash(value)
            bucket = buckets.setdefault(
                key,
                {"raw_value": value, "count": 0},
            )
            bucket["count"] += 1
        expose_literals = len(buckets) <= min(max_values, MAX_LITERAL_VALUE_BUCKETS)
        ordered = sorted(
            (
                {
                    "value": summarize_redacted_value(
                        bucket["raw_value"],
                        expose_literal=expose_literals,
                    ),
                    "count": bucket["count"],
                }
                for bucket in buckets.values()
            ),
            key=lambda item: (-int(item["count"]), str(item["value"].get("hash", ""))),
        )
        return {
            "tool": "profile_path_values",
            "db_id": self.db_id,
            "collection": collection_name,
            "path": target_path,
            "document_count": len(docs),
            "value_count": len(values),
            "total_values": len(values),
            "unique_value_count": len(buckets),
            "values": ordered[:max_values],
            "value_limit": max_values,
            "redaction": {
                "raw_rows": False,
                "scalar_values": "bounded_enum_literals" if expose_literals else "hash_only",
            },
        }

    def search_values(
        self,
        collection: str,
        query: str,
        *,
        limit: int | None = None,
        value_limit: int | None = None,
    ) -> dict[str, Any]:
        docs = self._sample(collection, bounded_limit(limit, default=DEFAULT_DOC_LIMIT,
                                                      maximum=MAX_DOC_LIMIT))
        max_values = bounded_limit(value_limit, default=DEFAULT_VALUE_LIMIT,
                                   maximum=MAX_VALUE_LIMIT)
        needle = str(query).lower()
        matches: list[dict[str, Any]] = []
        match_count = 0
        for doc_index, doc in enumerate(docs):
            for path, values in sorted(walk_document_paths(doc).items()):
                for value in values:
                    if not _value_matches(value, needle):
                        continue
                    match_count += 1
                    if len(matches) < max_values:
                        matches.append(
                            {
                                "path": path,
                                "document_offset": doc_index,
                                "value": summarize_redacted_value(value),
                            }
                        )
        return {
            "tool": "search_values",
            "db_id": self.db_id,
            "collection": collection,
            "query_hash": stable_hash(query),
            "document_count": len(docs),
            "match_count": match_count,
            "matches": matches,
            "value_limit": max_values,
            "redaction": {"raw_rows": False},
        }

    def inspect_array_shape(
        self,
        collection: str,
        path: str,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        docs = self._sample(collection, bounded_limit(limit, default=DEFAULT_DOC_LIMIT,
                                                      maximum=MAX_DOC_LIMIT))
        arrays: list[list[Any]] = []
        for doc in docs:
            arrays.extend(
                value
                for value in extract_path_values(doc, path)
                if isinstance(value, list)
            )
        lengths = [len(array) for array in arrays]
        element_values: list[Any] = []
        object_path_values: dict[str, list[Any]] = {}
        for array in arrays:
            for item in array:
                element_values.append(item)
                if isinstance(item, dict):
                    for item_path, values in walk_document_paths(item).items():
                        object_path_values.setdefault(item_path, []).extend(values)
        return {
            "tool": "inspect_array_shape",
            "db_id": self.db_id,
            "collection": collection,
            "path": path,
            "document_count": len(docs),
            "array_count": len(arrays),
            "min_length": min(lengths) if lengths else 0,
            "max_length": max(lengths) if lengths else 0,
            "element_type_counts": summarize_type_counts(element_values),
            "object_paths": _bounded_path_summary(object_path_values, limit=MAX_SAMPLE_PATHS),
            "object_path_count": len(object_path_values),
            "returned_object_path_count": min(len(object_path_values), MAX_SAMPLE_PATHS),
            "omitted_object_path_count": max(0, len(object_path_values) - MAX_SAMPLE_PATHS),
            "redaction": {"raw_rows": False},
        }

    def inspect_dynamic_keys(
        self,
        collection: str,
        path: str,
        *,
        limit: int | None = None,
        key_limit: int | None = None,
    ) -> dict[str, Any]:
        docs = self._sample(collection, bounded_limit(limit, default=DEFAULT_DOC_LIMIT,
                                                      maximum=MAX_DOC_LIMIT))
        max_keys = bounded_limit(key_limit, default=DEFAULT_VALUE_LIMIT, maximum=MAX_VALUE_LIMIT)
        objects: list[dict[str, Any]] = []
        for doc in docs:
            objects.extend(
                value
                for value in flatten_extracted_values(extract_path_values(doc, path))
                if isinstance(value, dict)
            )
        keys: list[str] = []
        values: list[Any] = []
        for obj in objects:
            for key, value in obj.items():
                keys.append(str(key))
                values.append(value)
        key_counts = Counter(keys)
        ordered_keys = sorted(key_counts, key=lambda key: (-key_counts[key], stable_hash(key)))
        return {
            "tool": "inspect_dynamic_keys",
            "db_id": self.db_id,
            "collection": collection,
            "path": path,
            "document_count": len(docs),
            "object_count": len(objects),
            "key_count": len(keys),
            "unique_key_count": len(key_counts),
            "key_samples": [summarize_redacted_value(key) for key in ordered_keys[:max_keys]],
            "value_type_counts": summarize_type_counts(values),
            "key_limit": max_keys,
            "redaction": {"raw_rows": False},
        }

    def profile_relationship_candidates(self, *, limit: int | None = None) -> dict[str, Any]:
        sample_limit = bounded_limit(limit, default=DEFAULT_DOC_LIMIT, maximum=MAX_DOC_LIMIT)
        collections = [
            str(item.get("collection") if isinstance(item, dict) else item)
            for item in self.list_collections()["collections"]
            if item
        ]
        indexes: dict[str, dict[str, dict[str, Any]]] = {}
        for collection in collections:
            docs = self._sample(collection, sample_limit)
            path_values: dict[str, list[Any]] = {}
            for doc in docs:
                for path, values in walk_document_paths(doc).items():
                    if path == "_id" or path.endswith("_id"):
                        scalar_values = [
                            value
                            for value in values
                            if value_kind(value) not in {"object", "array", "null"}
                        ]
                        if scalar_values:
                            path_values.setdefault(path, []).extend(scalar_values)
            indexes[collection] = {
                path: {
                    "count": len(values),
                    "hashes": [stable_hash(value) for value in values],
                }
                for path, values in sorted(path_values.items())
            }
        candidates: list[dict[str, Any]] = []
        for from_collection, from_paths in sorted(indexes.items()):
            for from_path, from_info in sorted(from_paths.items()):
                if from_path == "_id":
                    continue
                from_hashes = list(from_info["hashes"])
                if not from_hashes:
                    continue
                for to_collection, to_paths in sorted(indexes.items()):
                    if to_collection == from_collection or "_id" not in to_paths:
                        continue
                    to_hashes = set(to_paths["_id"]["hashes"])
                    match_count = sum(1 for value_hash in from_hashes if value_hash in to_hashes)
                    if match_count <= 0:
                        continue
                    candidates.append(
                        {
                            "from_collection": from_collection,
                            "from_path": from_path,
                            "to_collection": to_collection,
                            "to_path": "_id",
                            "sampled_from_values": len(from_hashes),
                            "sampled_to_values": int(to_paths["_id"]["count"]),
                            "match_count": match_count,
                            "confidence": round(match_count / max(1, len(from_hashes)), 3),
                        }
                    )
        candidates.sort(
            key=lambda item: (
                -float(item["confidence"]),
                -int(item["match_count"]),
                str(item["from_collection"]),
                str(item["from_path"]),
            )
        )
        return {
            "tool": "profile_relationship_candidates",
            "db_id": self.db_id,
            "collection_count": len(collections),
            "sample_limit": sample_limit,
            "candidates": candidates[:MAX_VALUE_LIMIT],
            "redaction": {"raw_rows": False},
        }

    def run_readonly_probe(
        self,
        mql: str | Mapping[str, Any],
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        request = mql if isinstance(mql, Mapping) else {}
        mql_text = _probe_mql_text(request, mql)
        probe_limit = bounded_limit(
            request.get("limit", limit) if request else limit,
            default=DEFAULT_DOC_LIMIT,
            maximum=MAX_DOC_LIMIT,
        )
        disabled = _disabled_hits_in_mql(mql_text)
        if disabled:
            raise ValueError(f"disabled operator in readonly probe: {disabled}")
        if hasattr(self.mongo, "run_readonly_probe"):
            result = self.mongo.run_readonly_probe(self.db_id, mql_text, limit=probe_limit)
        else:
            result = self.mongo.aggregate_readonly_bounded(
                self.db_id,
                mql_text,
                limit=probe_limit,
            )
            if isinstance(result, dict) and "sample" in result:
                result = dict(result)
                result["redacted_sample_shape"] = redact_value(result.pop("sample"))
        result["tool"] = "run_readonly_probe"
        result.setdefault("redaction", {"raw_rows": False})
        return result

    def _sample(self, collection: str | Mapping[str, Any], limit: int) -> list[dict[str, Any]]:
        if isinstance(collection, Mapping):
            collection = str(collection.get("collection", ""))
        raw_sampler = getattr(self.mongo, "_sample_documents_raw", None)
        if callable(raw_sampler):
            docs = raw_sampler(self.db_id, collection, limit=limit)
        else:
            docs = self.mongo.sample_documents(self.db_id, collection, limit=limit)
        if isinstance(docs, dict):
            return []
        return [doc for doc in docs if isinstance(doc, dict)]


def _value_matches(value: Any, needle: str) -> bool:
    if value_kind(value) in {"object", "array", "null"}:
        return False
    return needle in str(value).lower()


def _bounded_path_summary(
    path_values: dict[str, list[Any]],
    *,
    limit: int,
) -> dict[str, dict[str, Any]]:
    selected = sorted(
        path_values.items(),
        key=lambda item: (-len(item[1]), item[0].count("."), item[0]),
    )[:limit]
    return {
        path: {
            "value_count": len(values),
            "type_counts": summarize_type_counts(values),
        }
        for path, values in selected
    }


def _path_kind_samples(
    path_values: dict[str, list[Any]],
    kind: str,
    *,
    limit: int,
) -> list[str]:
    paths = [
        path
        for path, values in sorted(path_values.items())
        if any(value_kind(value) == kind for value in values)
    ]
    return paths[:limit]


def _dynamic_key_candidates(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys_by_path: dict[str, Counter[str]] = {}
    for doc in docs:
        _collect_mapping_keys(doc, (), keys_by_path)
    candidates: list[dict[str, Any]] = []
    for path, counts in sorted(keys_by_path.items()):
        if len(counts) < 3:
            continue
        ordered_keys = sorted(counts, key=lambda key: (-counts[key], stable_hash(key)))
        candidates.append(
            {
                "path": path,
                "unique_key_count": len(counts),
                "total_key_occurrences": sum(counts.values()),
                "key_samples": [
                    summarize_redacted_value(key)
                    for key in ordered_keys[:MAX_DYNAMIC_KEY_SAMPLES]
                ],
            }
        )
    candidates.sort(
        key=lambda item: (
            -int(item["unique_key_count"]),
            -int(item["total_key_occurrences"]),
            str(item["path"]),
        )
    )
    return candidates[:MAX_DYNAMIC_KEY_PATHS]


def _collect_mapping_keys(
    value: Any,
    path: tuple[str, ...],
    keys_by_path: dict[str, Counter[str]],
) -> None:
    if isinstance(value, Mapping):
        if path:
            key = _format_path(path)
            bucket = keys_by_path.setdefault(key, Counter())
            for child_key in value:
                bucket[str(child_key)] += 1
        for child_key, child in value.items():
            _collect_mapping_keys(child, path + (str(child_key),), keys_by_path)
        return
    if isinstance(value, list):
        for item in value:
            _collect_mapping_keys(item, path + ("[]",), keys_by_path)


def _format_path(path: tuple[str, ...]) -> str:
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


SmartEGMongoTools = SmartEgMongoTools


def _disabled_hits_in_mql(mql: str) -> list[str]:
    try:
        return scan_disabled(mql)
    except Exception:
        return []


def _probe_mql_text(request: Mapping[str, Any], mql: Any) -> str:
    if not request:
        if isinstance(mql, str) and mql.strip():
            return mql
        raise ValueError("readonly probe requires MQL or collection and pipeline")

    candidate = request.get("MQL") or request.get("mql")
    collection = str(request.get("collection") or "").strip()
    pipeline = request.get("pipeline")
    if pipeline is None:
        pipeline = request.get("stages")

    if isinstance(candidate, str) and candidate.strip().startswith("["):
        pipeline = _parse_pipeline_json(candidate)
        candidate = None
    if isinstance(candidate, str) and candidate.strip():
        return candidate

    if not collection:
        raise ValueError("readonly probe collection is required when MQL is omitted")
    if not isinstance(pipeline, list) or not all(isinstance(stage, dict) for stage in pipeline):
        raise ValueError("readonly probe pipeline must be a list of stage objects")
    return render_mql(collection, pipeline)


def _parse_pipeline_json(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("readonly probe raw pipeline string must be valid JSON") from exc
    if not isinstance(parsed, list) or not all(isinstance(stage, dict) for stage in parsed):
        raise ValueError("readonly probe raw pipeline string must decode to stage objects")
    return parsed
