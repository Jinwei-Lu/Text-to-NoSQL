"""Import TEND mongodb_data snapshots and measure document shape heterogeneity."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pymongo import MongoClient


def load_mongodb_data_snapshot(path: Path) -> dict[str, list[dict[str, Any]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected object at root of {path}")
    out: dict[str, list[dict[str, Any]]] = {}
    for coll, docs in raw.items():
        if not isinstance(docs, list):
            raise ValueError(f"Collection {coll!r} in {path} must be a list")
        out[coll] = docs
    return out


def document_shape(doc: Any, *, prefix: str = "") -> frozenset[tuple[str, str]]:
    """Leaf field paths + BSON-ish type labels (schema-less fingerprint)."""
    shapes: set[tuple[str, str]] = set()
    if isinstance(doc, dict):
        for key, value in doc.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                shapes |= set(document_shape(value, prefix=path))
            elif isinstance(value, list):
                shapes.add((path, "array"))
                for item in value[:5]:
                    if isinstance(item, dict):
                        shapes |= set(document_shape(item, prefix=f"{path}[]"))
            elif value is None:
                shapes.add((path, "null"))
            elif isinstance(value, bool):
                shapes.add((path, "bool"))
            elif isinstance(value, int):
                shapes.add((path, "int"))
            elif isinstance(value, float):
                shapes.add((path, "float"))
            else:
                shapes.add((path, type(value).__name__))
    return frozenset(shapes)


def top_level_keys(doc: dict[str, Any]) -> frozenset[str]:
    return frozenset(doc.keys())


@dataclass
class CollectionShapeReport:
    db_id: str
    collection: str
    document_count: int
    unique_shapes: int
    unique_top_level_keysets: int
    modal_shape_count: int
    modal_shape_share: float
    heterogeneous_document_count: int
    heterogeneous_ratio: float
    shape_examples: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_id": self.db_id,
            "collection": self.collection,
            "document_count": self.document_count,
            "unique_shapes": self.unique_shapes,
            "unique_top_level_keysets": self.unique_top_level_keysets,
            "modal_shape_share": round(self.modal_shape_share, 4),
            "heterogeneous_document_count": self.heterogeneous_document_count,
            "heterogeneous_ratio": round(self.heterogeneous_ratio, 4),
            "shape_examples": self.shape_examples,
        }


def analyze_collection_shapes(
    db_id: str,
    collection: str,
    documents: list[dict[str, Any]],
    *,
    max_shape_examples: int = 5,
) -> CollectionShapeReport:
    if not documents:
        return CollectionShapeReport(
            db_id=db_id,
            collection=collection,
            document_count=0,
            unique_shapes=0,
            unique_top_level_keysets=0,
            modal_shape_count=0,
            modal_shape_share=0.0,
            heterogeneous_document_count=0,
            heterogeneous_ratio=0.0,
        )

    shape_counter: Counter[frozenset[tuple[str, str]]] = Counter()
    keyset_counter: Counter[frozenset[str]] = Counter()
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        shape_counter[document_shape(doc)] += 1
        keyset_counter[top_level_keys(doc)] += 1

    modal_shape, modal_count = shape_counter.most_common(1)[0]
    total = sum(shape_counter.values())
    heterogeneous = total - modal_count

    examples: dict[str, int] = {}
    for shape, count in shape_counter.most_common(max_shape_examples):
        label = ", ".join(sorted(f"{p}:{t}" for p, t in shape))[:200]
        examples[label or "(empty)"] = count

    return CollectionShapeReport(
        db_id=db_id,
        collection=collection,
        document_count=total,
        unique_shapes=len(shape_counter),
        unique_top_level_keysets=len(keyset_counter),
        modal_shape_count=modal_count,
        modal_shape_share=modal_count / total if total else 0.0,
        heterogeneous_document_count=heterogeneous,
        heterogeneous_ratio=heterogeneous / total if total else 0.0,
        shape_examples=examples,
    )


@dataclass
class ImportReport:
    mongo_uri: str
    database_prefix: str
    db_reports: list[CollectionShapeReport] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        if not self.db_reports:
            return {"collections": 0}
        total_docs = sum(r.document_count for r in self.db_reports)
        total_hetero = sum(r.heterogeneous_document_count for r in self.db_reports)
        flex_collections = [
            r for r in self.db_reports if r.unique_shapes > 1 or r.unique_top_level_keysets > 1
        ]
        return {
            "collections_analyzed": len(self.db_reports),
            "total_documents": total_docs,
            "collections_with_multiple_shapes": len(flex_collections),
            "overall_heterogeneous_documents": total_hetero,
            "overall_heterogeneous_ratio": round(total_hetero / total_docs, 4) if total_docs else 0.0,
            "per_collection": [r.to_dict() for r in self.db_reports],
        }


def import_release_to_mongo(
    release_root: Path,
    *,
    mongo_uri: str,
    database_prefix: str = "tend",
    drop_existing: bool = True,
) -> ImportReport:
    data_dir = release_root / "mongodb_data"
    if not data_dir.is_dir():
        raise FileNotFoundError(f"mongodb_data not found under {release_root}")

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")

    report = ImportReport(mongo_uri=mongo_uri, database_prefix=database_prefix)

    for data_path in sorted(data_dir.glob("*.json")):
        db_id = data_path.stem
        snapshot = load_mongodb_data_snapshot(data_path)
        db_name = f"{database_prefix}_{db_id}"
        db = client[db_name]
        if drop_existing:
            client.drop_database(db_name)

        for collection_name, documents in snapshot.items():
            coll = db[collection_name]
            if documents:
                coll.insert_many(documents)
            else:
                db.create_collection(collection_name)
            report.db_reports.append(
                analyze_collection_shapes(db_id, collection_name, documents)
            )

    client.close()
    return report
