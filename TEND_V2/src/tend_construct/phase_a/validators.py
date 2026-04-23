from __future__ import annotations

from typing import Any

from tend_core import validate_phenomena_registry, validate_schema_data_alignment
from tend_core.signatures import schema_signature, world_signature


def validate_phase_a_bundle(
    schema_payload: dict[str, Any],
    data_payload: dict[str, Any],
    registry_payload: dict[str, Any],
) -> list[str]:
    """Run S1-S8 structural validations on a Phase A bundle."""
    errors: list[str] = []

    # S1: schema/data collection alignment
    errors.extend(validate_schema_data_alignment(schema_payload, data_payload))

    # S2: phenomena registry references valid collections/documents
    errors.extend(validate_phenomena_registry(schema_payload, data_payload, registry_payload))

    # S3: schema_signature integrity
    expected_schema_sig = schema_signature(schema_payload)
    actual_schema_sig = registry_payload.get("_meta", {}).get("schema_signature")
    if actual_schema_sig and actual_schema_sig != expected_schema_sig:
        errors.append("S3: schema_signature mismatch")

    # S4: world_signature integrity
    expected_world_sig = world_signature(data_payload)
    if registry_payload.get("world_signature") != expected_world_sig:
        errors.append("S4: world_signature mismatch")

    # S5: minimum document count per collection
    for collection_name, documents in data_payload.items():
        if isinstance(documents, list) and len(documents) < 5:
            errors.append(f"S5: {collection_name} has only {len(documents)} documents (min 5)")

    # S6: unique _id per collection
    for collection_name, documents in data_payload.items():
        if not isinstance(documents, list):
            continue
        ids = [doc.get("_id") for doc in documents if isinstance(doc, dict)]
        if len(ids) != len(set(str(i) for i in ids)):
            errors.append(f"S6: duplicate _id in {collection_name}")

    # S7: phenomena must have at least one associated document
    for phenom in registry_payload.get("phenomena", []):
        evidence = phenom.get("witness_evidence", {})
        doc_ids = evidence.get("document_ids", [])
        if not doc_ids:
            errors.append(f"S7: phenomenon {phenom.get('phenomenon_id', '?')} has no document_ids")

    # S8: all schema fields must be present in at least one document
    for collection_name, spec in schema_payload.items():
        fields = spec.get("fields", {})
        documents = data_payload.get(collection_name, [])
        if not documents:
            continue
        all_keys: set[str] = set()
        for doc in documents:
            if isinstance(doc, dict):
                all_keys.update(doc.keys())
        for field_name in fields:
            if field_name.startswith("_"):
                continue
            if field_name not in all_keys:
                if not fields[field_name].get("sparse", False):
                    errors.append(f"S8: field {collection_name}.{field_name} absent from all documents")

    return errors


def validate_constructed_records(
    records: list[dict[str, Any]],
    schema_dir_contents: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Run C1-C9 validations on constructed records."""
    errors: list[str] = []

    # C1: unique record_id
    record_ids = [r.get("record_id") for r in records]
    if len(record_ids) != len(set(record_ids)):
        errors.append("C1: duplicate record_id found")

    # C2: every record has required fields
    required_fields = {"record_id", "db_id", "nl_queries", "MQL", "canonical_form_set"}
    for r in records:
        missing = required_fields - set(r.keys())
        if missing:
            errors.append(f"C2: record {r.get('record_id', '?')} missing fields: {missing}")

    # C3: nl_queries has exactly 5 entries (L0-L4)
    for r in records:
        nls = r.get("nl_queries", [])
        if len(nls) != 5:
            errors.append(f"C3: record {r.get('record_id', '?')} has {len(nls)} NL queries (expected 5)")

    # C4: MQL must parse_ok
    from tend_core.mql import parse_ok
    for r in records:
        mql = r.get("MQL", "")
        if not parse_ok(mql):
            errors.append(f"C4: record {r.get('record_id', '?')} MQL does not parse")

    # C5: canonical_form_set.known_variants must include at least the gold
    from tend_core.mql import canonical_text
    for r in records:
        cfs = r.get("canonical_form_set", {})
        variants = cfs.get("known_variants", [])
        gold_canonical = canonical_text(r.get("MQL", ""))
        if gold_canonical and gold_canonical not in variants:
            errors.append(f"C5: record {r.get('record_id', '?')} gold not in known_variants")

    # C6: operator_family must be set
    for r in records:
        if not r.get("operator_family"):
            errors.append(f"C6: record {r.get('record_id', '?')} missing operator_family")

    # C7: world_signature must be set
    for r in records:
        if not r.get("world_signature"):
            errors.append(f"C7: record {r.get('record_id', '?')} missing world_signature")

    # C8: db_id must reference an existing schema
    if schema_dir_contents:
        for r in records:
            db_id = r.get("db_id", "")
            domain = db_id.rsplit("_", 1)[0] if "_" in db_id else db_id
            if db_id not in schema_dir_contents:
                errors.append(f"C8: record {r.get('record_id', '?')} references unknown db_id {db_id}")

    # C9: AST check must pass for gold query
    from tend_core.checks import ast_check
    from tend_core.models import CanonicalFormSet
    for r in records:
        cfs_dict = r.get("canonical_form_set", {})
        cfs = CanonicalFormSet.from_dict(cfs_dict)
        mql = r.get("MQL", "")
        if parse_ok(mql):
            result = ast_check(mql, cfs)
            if result != "pass":
                errors.append(f"C9: record {r.get('record_id', '?')} AST check failed: {result}")

    return errors
