from __future__ import annotations

from typing import Any

from .mql import extract_operator_tokens, extract_root_stage_tokens, parse_ok
from .models import CanonicalFormSet


def ast_check(query: str, canonical_form_set: CanonicalFormSet) -> str:
    if not parse_ok(query):
        return "fail:parse_error"

    tokens_all = set(extract_operator_tokens(query))
    tokens_root = set(extract_root_stage_tokens(query))

    for token in canonical_form_set.must_contain:
        if token not in tokens_all:
            return f"fail:missing:{token}"
    for token in canonical_form_set.must_contain_at_root:
        if token not in tokens_root:
            return f"fail:missing_at_root:{token}"
    for token in canonical_form_set.must_not_contain:
        if token in tokens_all:
            return f"fail:forbidden:{token}"
    for token in canonical_form_set.must_not_contain_at_root:
        if token in tokens_root:
            return f"fail:forbidden_at_root:{token}"
    return "pass"


def validate_schema_data_alignment(schema: dict[str, Any], data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if set(schema) != set(data):
        errors.append("schema/data top-level collection mismatch")
    for collection_name, documents in data.items():
        if not isinstance(documents, list):
            errors.append(f"{collection_name} is not a document list")
            continue
        if not documents:
            errors.append(f"{collection_name} is empty")
            continue
        for document in documents:
            if not isinstance(document, dict):
                errors.append(f"{collection_name} contains a non-object document")
                break
            if "_id" not in document:
                errors.append(f"{collection_name} contains a document without _id")
                break
    return errors


def validate_phenomena_registry(schema: dict[str, Any], data: dict[str, Any], registry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    phenomena = registry.get("phenomena", [])
    data_index = {
        f"{collection}/{document['_id']}": document
        for collection, documents in data.items()
        for document in documents
        if isinstance(document, dict) and "_id" in document
    }

    for phenomenon in phenomena:
        evidence = phenomenon.get("witness_evidence", {})
        collection = evidence.get("collection")
        path = evidence.get("path")
        document_ids = evidence.get("document_ids", [])
        if collection not in schema:
            errors.append(f"phenomenon references unknown collection {collection}")
        if not path:
            errors.append("phenomenon evidence path is missing")
        for document_id in document_ids:
            if document_id not in data_index:
                errors.append(f"phenomenon references missing document {document_id}")
    return errors
