from __future__ import annotations

from typing import Any

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
