from __future__ import annotations

from collections import defaultdict
from typing import Any

from ...execution import world_signature as compute_world_signature
from ..native_executor import NativeExecutionResult
from ..native_recipe import NativeFeature, NativeFeatureManifest
from ..native_recipe import NativeMigrationRecipe
from .common import collection, expr, join, recipe, source as field_source, transform

DESIGN_VERSION = 1
MODULE_REF = __name__


def build_native_recipe(source: Any, db_id: str) -> NativeMigrationRecipe:
    source.schema(db_id)
    return recipe(
        db_id,
        version=DESIGN_VERSION,
        design_goal=(
            "Represent Magic card printings with dynamic legality maps, rulings "
            "timelines, and typed card/set entities."
        ),
        collections=[
            collection(
                "card_print_profiles",
                purpose="Card printing documents with format-keyed legality state.",
                source_tables=["cards", "legalities", "rulings", "foreign_data"],
                transforms=[
                    transform(
                        "legality_by_format",
                        "dynamic_key_object",
                        module_ref=MODULE_REF,
                        parent_table="cards",
                        child_table="legalities",
                        join=join("cards.uuid", "legalities.uuid"),
                        target_field="legality_by_format",
                        key=expr("legalities.format", "legalities.format"),
                        values={
                            "status": expr("last(legalities.status)", "legalities.status"),
                            "format_count": expr("count(legalities.id)", "legalities.id"),
                        },
                    ),
                    transform(
                        "card_ruling_events",
                        "nested_event_stream",
                        module_ref=MODULE_REF,
                        parent_table="cards",
                        event_source_table="rulings",
                        join=join("cards.uuid", "rulings.uuid"),
                        target_field="rulings",
                        event_type_field="rulings.text",
                        event_time_field="rulings.date",
                        event_payload={
                            "ruling_id": "rulings.id",
                            "text": "rulings.text",
                        },
                    ),
                    transform(
                        "card_print_tags",
                        "derived_tag_array",
                        module_ref=MODULE_REF,
                        target_field="print_tags",
                        tags={
                            "mythic": {
                                "condition": "cards.rarity == 'mythic'",
                                "provenance": ["cards.rarity"],
                            },
                            "borderless": {
                                "condition": "cards.borderColor == 'borderless'",
                                "provenance": ["cards.borderColor"],
                            },
                            "has_powerful_foil_ids": {
                                "condition": (
                                    "cards.cardKingdomFoilId is not null and "
                                    "cards.cardKingdomId is not null"
                                ),
                                "provenance": [
                                    "cards.cardKingdomFoilId",
                                    "cards.cardKingdomId",
                                ],
                            },
                        },
                    ),
                ],
            ),
            collection(
                "card_catalog_entities",
                purpose="Typed card and set records for native catalog search.",
                source_tables=["cards", "sets", "set_translations"],
                transforms=[
                    transform(
                        "catalog_entity_union",
                        "polymorphic_union",
                        module_ref=MODULE_REF,
                        discriminator="entity_type",
                        variants={
                            "card": {
                                "source_table": "cards",
                                "fields": {
                                    "entity_id": expr("concat('card:', cards.uuid)", "cards.uuid"),
                                    "name": field_source("cards.name"),
                                    "set_code": field_source("cards.setCode"),
                                    "layout": field_source("cards.layout"),
                                },
                            },
                            "set": {
                                "source_table": "sets",
                                "fields": {
                                    "entity_id": expr("concat('set:', sets.code)", "sets.code"),
                                    "name": field_source("sets.name"),
                                    "released": field_source("sets.releaseDate"),
                                    "set_type": field_source("sets.type"),
                                },
                            },
                            "set_translation": {
                                "source_table": "set_translations",
                                "fields": {
                                    "entity_id": expr(
                                        "concat('translation:', set_translations.id)",
                                        "set_translations.id",
                                    ),
                                    "language": field_source("set_translations.language"),
                                    "translation": field_source("set_translations.translation"),
                                },
                            },
                        },
                    )
                ],
            ),
        ],
    )


def materialize_native_dataworld(
    source: Any,
    db_id: str,
    *,
    event_hook: Any = None,
) -> NativeExecutionResult:
    if db_id != "card_games":
        raise ValueError(f"card_games materializer received db_id={db_id!r}")
    schema = source.schema(db_id)
    conn = source.connection(db_id)

    cards = _rows(conn, "cards", ["id"])
    sets = _rows(conn, "sets", ["code"])
    legalities_by_uuid = _group(_rows(conn, "legalities", ["uuid", "format", "id"]), "uuid")
    foreign_by_uuid = _group(_rows(conn, "foreign_data", ["uuid", "language", "id"]), "uuid")
    rulings_by_uuid = _group(_rows(conn, "rulings", ["uuid", "date", "id"]), "uuid")
    translations_by_set = _group(_rows(conn, "set_translations", ["setCode", "language", "id"]), "setCode")
    set_by_code = {row.get("code"): row for row in sets}
    cards_by_set = _group(cards, "setCode")

    card_docs = [
        _card_print_dossier(
            card,
            set_by_code.get(card.get("setCode"), {}),
            legalities_by_uuid.get(card.get("uuid"), []),
            foreign_by_uuid.get(card.get("uuid"), []),
            rulings_by_uuid.get(card.get("uuid"), []),
        )
        for card in cards
    ]
    set_docs = [
        _set_release_ecosystem(
            set_row,
            cards_by_set.get(set_row.get("code"), []),
            translations_by_set.get(set_row.get("code"), []),
        )
        for set_row in sets
    ]
    data = {
        "card_print_dossiers": card_docs,
        "set_release_ecosystems": set_docs,
    }
    manifest = NativeFeatureManifest(db_id=db_id, features=_native_features())
    native_schema = {
        "db_id": db_id,
        "source_tables": list(schema.tables),
        "collections": {
            "card_print_dossiers": {
                "document_count": len(card_docs),
                "root_entity": "card printing",
                "source_tables": ["cards", "legalities", "foreign_data", "rulings", "sets"],
            },
            "set_release_ecosystems": {
                "document_count": len(set_docs),
                "root_entity": "card set release",
                "source_tables": ["sets", "cards", "set_translations"],
            },
        },
    }
    provenance = {
        feature.id: {
            "module": MODULE_REF,
            "source_tables": feature.provenance_refs,
            "field": feature.field,
        }
        for feature in manifest.features
    }
    signature = compute_world_signature(data)
    if event_hook is not None:
        event_hook(
            "card_games_native_materialized",
            db_id=db_id,
            collection_count=len(data),
            document_count=sum(len(docs) for docs in data.values()),
            native_feature_count=len(manifest.features),
            world_signature=signature,
        )
    return NativeExecutionResult(
        data=data,
        schema=native_schema,
        manifest=manifest,
        provenance=provenance,
        world_signature=signature,
    )


def _rows(conn: Any, table: str, order_by: list[str]) -> list[dict[str, Any]]:
    order_sql = ", ".join(f'"{name}"' for name in order_by)
    cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY {order_sql}')
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _group(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    out: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[row.get(key)].append(row)
    return dict(out)


def _card_print_dossier(
    card: dict[str, Any],
    set_row: dict[str, Any],
    legalities: list[dict[str, Any]],
    foreign_rows: list[dict[str, Any]],
    rulings: list[dict[str, Any]],
) -> dict[str, Any]:
    legality_by_format = _legality_by_format(legalities)
    translations_by_language = _translations_by_language(foreign_rows)
    rulings_by_year = _rulings_by_year(rulings)
    return {
        "_id": card.get("uuid") or f"card:{card.get('id')}",
        "card_id": card.get("id"),
        "print_identity": {
            "uuid": card.get("uuid"),
            "name": card.get("name"),
            "ascii_name": card.get("asciiName"),
            "set": {
                "code": card.get("setCode"),
                "name": set_row.get("name"),
                "release_date": set_row.get("releaseDate"),
                "type": set_row.get("type"),
            },
            "number": card.get("number"),
            "artist": card.get("artist"),
            "rarity": card.get("rarity"),
            "layout": card.get("layout"),
            "side": {"value": card.get("side"), "state": _presence(card.get("side"))},
        },
        "gameplay": {
            "mana": {
                "cost": card.get("manaCost"),
                "converted": card.get("convertedManaCost"),
                "colors": _split_csv(card.get("colors")),
                "color_identity": _split_csv(card.get("colorIdentity")),
            },
            "type_line": {
                "oracle": card.get("type"),
                "original": card.get("originalType"),
                "types": _split_csv(card.get("types")),
                "subtypes": _split_csv(card.get("subtypes")),
                "supertypes": _split_csv(card.get("supertypes")),
            },
            "combat": {
                "power": {"value": card.get("power"), "state": _presence(card.get("power"))},
                "toughness": {"value": card.get("toughness"), "state": _presence(card.get("toughness"))},
                "loyalty": {"value": card.get("loyalty"), "state": _presence(card.get("loyalty"))},
            },
            "rules_text": {
                "oracle": {"value": card.get("text"), "state": _presence(card.get("text"))},
                "original": {
                    "value": card.get("originalText"),
                    "state": _presence(card.get("originalText")),
                },
                "keywords": _split_csv(card.get("keywords")),
            },
        },
        "print_features": {
            "availability": _split_csv(card.get("availability")),
            "border_color": card.get("borderColor"),
            "flags_by_name": {
                "has_foil": {"value": bool(card.get("hasFoil")), "state": _presence(card.get("hasFoil"))},
                "has_nonfoil": {
                    "value": bool(card.get("hasNonFoil")),
                    "state": _presence(card.get("hasNonFoil")),
                },
                "is_promo": {"value": bool(card.get("isPromo")), "state": _presence(card.get("isPromo"))},
                "is_reprint": {"value": bool(card.get("isReprint")), "state": _presence(card.get("isReprint"))},
                "content_warning": {
                    "value": bool(card.get("hasContentWarning")),
                    "state": _presence(card.get("hasContentWarning")),
                },
            },
            "tags": _print_tags(card),
        },
        "legality": {
            "by_format": legality_by_format,
            "format_count": len(legality_by_format),
        },
        "localization": {
            "translations_by_language": translations_by_language,
            "language_count": len(translations_by_language),
        },
        "rulings": {
            "by_year": rulings_by_year,
            "ruling_count": sum(len(bucket["events"]) for bucket in rulings_by_year.values()),
        },
        "views": [
            {
                "view_id": "constructed_legality",
                "status_by_format": {
                    fmt: {
                        "status": bucket["latest_status"],
                        "status_presence_state": bucket["status_presence_state"],
                    }
                    for fmt, bucket in legality_by_format.items()
                },
            },
            {
                "view_id": "translation_catalog",
                "translations_by_language": translations_by_language,
            },
        ],
        "schema_state": {
            "legalities": _presence(legalities),
            "foreign_data": _presence(foreign_rows),
            "rulings": _presence(rulings),
            "power": _presence(card.get("power")),
            "digital_faces": "missing",
        },
    }


def _set_release_ecosystem(
    set_row: dict[str, Any],
    cards: list[dict[str, Any]],
    translations: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "_id": f"set:{set_row.get('code')}",
        "set_code": set_row.get("code"),
        "release": {
            "name": set_row.get("name"),
            "type": set_row.get("type"),
            "block": {"value": set_row.get("block"), "state": _presence(set_row.get("block"))},
            "date": set_row.get("releaseDate"),
            "sizes": {
                "base": set_row.get("baseSetSize"),
                "total": set_row.get("totalSetSize"),
            },
            "foil_policy": {
                "foil_only": bool(set_row.get("isFoilOnly")),
                "foreign_only": bool(set_row.get("isForeignOnly")),
                "online_only": bool(set_row.get("isOnlineOnly")),
            },
        },
        "cards_by_rarity": _cards_by_rarity(cards),
        "translations_by_language": {
            _dynamic_key(row.get("language"), "unknown"): {
                "translation": row.get("translation"),
                "translation_presence_state": _presence(row.get("translation")),
            }
            for row in translations
        },
        "schema_state": {
            "cards": _presence(cards),
            "translations": _presence(translations),
            "booster_schema": _presence(set_row.get("booster")),
            "parent_code": _presence(set_row.get("parentCode")),
        },
    }


def _legality_by_format(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        fmt = _dynamic_key(row.get("format"), "unknown")
        out.setdefault(
            fmt,
            {
                "latest_status": row.get("status"),
                "status_presence_state": _presence(row.get("status")),
                "events": [],
            },
        )
        out[fmt]["events"].append(
            {
                "legality_id": row.get("id"),
                "format": row.get("format"),
                "status": row.get("status"),
                "status_presence_state": _presence(row.get("status")),
            }
        )
    return dict(sorted(out.items()))


def _translations_by_language(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[_dynamic_key(row.get("language"), "unknown")].append(
            {
                "foreign_data_id": row.get("id"),
                "name": row.get("name"),
                "text": {"value": row.get("text"), "state": _presence(row.get("text"))},
                "type": {"value": row.get("type"), "state": _presence(row.get("type"))},
                "multiverse_id": row.get("multiverseid"),
            }
        )
    return {key: value for key, value in sorted(out.items())}


def _rulings_by_year(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        date = str(row.get("date") or "")
        year = date[:4] if len(date) >= 4 else "unknown"
        out.setdefault(year, {"events": []})
        out[year]["events"].append(
            {
                "ruling_id": row.get("id"),
                "date": row.get("date"),
                "text": {"value": row.get("text"), "state": _presence(row.get("text"))},
            }
        )
    return dict(sorted(out.items()))


def _cards_by_rarity(cards: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for card in cards:
        rarity = _dynamic_key(card.get("rarity"), "unknown")
        out.setdefault(rarity, {"cards": []})
        out[rarity]["cards"].append(
            {
                "uuid": card.get("uuid"),
                "name": card.get("name"),
                "number": card.get("number"),
                "artist": card.get("artist"),
                "layout": card.get("layout"),
            }
        )
    return dict(sorted(out.items()))


def _split_csv(value: Any) -> list[str]:
    if not isinstance(value, str) or not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _presence(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return "empty" if value == "" else "present"
    if isinstance(value, (list, tuple, dict, set)):
        return "present" if len(value) > 0 else "empty"
    return "present"


def _dynamic_key(value: Any, fallback: str) -> str:
    text = str(value) if value is not None else fallback
    text = text.strip()
    return text if text else fallback


def _print_tags(card: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    if card.get("rarity") == "mythic":
        tags.append("mythic")
    if card.get("borderColor") == "borderless":
        tags.append("borderless")
    if card.get("cardKingdomFoilId") is not None and card.get("cardKingdomId") is not None:
        tags.append("has_powerful_foil_ids")
    if card.get("isPromo"):
        tags.append("promo")
    if card.get("hasContentWarning"):
        tags.append("content_warning")
    return tags


def _native_features() -> list[NativeFeature]:
    return [
        NativeFeature(
            id="card_print_dossiers.legality_by_format",
            type="dynamic_key_object",
            collection="card_print_dossiers",
            field="legality.by_format",
            query_patterns=["format legality status matrix"],
            required_constructs=["$objectToArray", "$unwind", "$group"],
            provenance_refs=["legalities.format", "legalities.status", "cards.uuid"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "format legality status matrix",
                        "intent": "summarize format-keyed legality states across card print dossiers",
                        "pipeline": [
                            {"$project": {"formats": {"$objectToArray": "$legality.by_format"}}},
                            {"$unwind": "$formats"},
                            {
                                "$group": {
                                    "_id": {
                                        "format": "$formats.k",
                                        "status": "$formats.v.latest_status",
                                    },
                                    "card_count": {"$sum": 1},
                                }
                            },
                            {"$sort": {"card_count": -1, "_id.format": 1, "_id.status": 1}},
                            {"$limit": 50},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$group"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="card_print_dossiers.rulings_by_year",
            type="dynamic_key_object",
            collection="card_print_dossiers",
            field="rulings.by_year",
            query_patterns=["ruling timeline by year"],
            required_constructs=["$objectToArray", "$unwind", "$size"],
            provenance_refs=["rulings.date", "rulings.text", "cards.uuid"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "ruling timeline by year",
                        "intent": "find card printings with year-keyed ruling timelines",
                        "pipeline": [
                            {"$project": {"name": "$print_identity.name", "years": {"$objectToArray": "$rulings.by_year"}}},
                            {"$unwind": "$years"},
                            {
                                "$project": {
                                    "name": 1,
                                    "year": "$years.k",
                                    "ruling_count": {"$size": "$years.v.events"},
                                }
                            },
                            {"$match": {"ruling_count": {"$gt": 0}}},
                            {"$sort": {"ruling_count": -1, "name": 1}},
                            {"$limit": 50},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$size"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="card_print_dossiers.localization_by_language",
            type="dynamic_key_object",
            collection="card_print_dossiers",
            field="localization.translations_by_language",
            query_patterns=["language localization coverage"],
            required_constructs=["$objectToArray", "$unwind", "$size"],
            provenance_refs=["foreign_data.language", "foreign_data.name", "cards.uuid"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "language localization coverage",
                        "intent": "count translated printings by language dynamic key",
                        "pipeline": [
                            {
                                "$project": {
                                    "translations": {
                                        "$objectToArray": "$localization.translations_by_language"
                                    }
                                }
                            },
                            {"$unwind": "$translations"},
                            {
                                "$group": {
                                    "_id": "$translations.k",
                                    "translated_printings": {"$sum": 1},
                                }
                            },
                            {"$sort": {"translated_printings": -1, "_id": 1}},
                            {"$limit": 50},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$group"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="set_release_ecosystems.cards_by_rarity",
            type="dynamic_key_object",
            collection="set_release_ecosystems",
            field="cards_by_rarity",
            query_patterns=["set rarity print mix"],
            required_constructs=["$objectToArray", "$unwind", "$size"],
            provenance_refs=["sets.code", "cards.rarity", "cards.uuid"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "set rarity print mix",
                        "intent": "summarize set releases by rarity-keyed card buckets",
                        "pipeline": [
                            {"$project": {"set_code": 1, "rarities": {"$objectToArray": "$cards_by_rarity"}}},
                            {"$unwind": "$rarities"},
                            {
                                "$project": {
                                    "set_code": 1,
                                    "rarity": "$rarities.k",
                                    "card_count": {"$size": "$rarities.v.cards"},
                                }
                            },
                            {"$sort": {"card_count": -1, "set_code": 1, "rarity": 1}},
                            {"$limit": 50},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$size"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="card_print_dossiers.digital_faces_presence",
            type="missing_vs_present",
            collection="card_print_dossiers",
            field="schema_state.digital_faces",
            query_patterns=["missing_vs_present"],
            required_constructs=["$ifNull"],
            provenance_refs=["cards.uuid"],
        ),
    ]
