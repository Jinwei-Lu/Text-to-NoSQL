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
            "Represent superheroes with attribute-id keyed ability maps, power "
            "collections, and typed hero/reference entities."
        ),
        collections=[
            collection(
                "hero_profiles",
                purpose="Hero documents with dynamic attribute maps and profile tags.",
                source_tables=["superhero", "hero_attribute", "hero_power"],
                transforms=[
                    transform(
                        "attributes_by_id",
                        "dynamic_key_object",
                        module_ref=MODULE_REF,
                        parent_table="superhero",
                        child_table="hero_attribute",
                        join=join("superhero.id", "hero_attribute.hero_id"),
                        target_field="attributes_by_id",
                        key=expr("hero_attribute.attribute_id", "hero_attribute.attribute_id"),
                        values={
                            "value": expr(
                                "max(hero_attribute.attribute_value)",
                                "hero_attribute.attribute_value",
                            ),
                            "observations": expr(
                                "count(hero_attribute.hero_id)",
                                "hero_attribute.hero_id",
                            ),
                        },
                    ),
                    transform(
                        "hero_profile_tags",
                        "derived_tag_array",
                        module_ref=MODULE_REF,
                        target_field="hero_tags",
                        tags={
                            "very_tall": {
                                "condition": "superhero.height_cm > 200",
                                "provenance": ["superhero.height_cm"],
                            },
                            "heavyweight": {
                                "condition": "superhero.weight_kg > 100",
                                "provenance": ["superhero.weight_kg"],
                            },
                            "publisher_known": {
                                "condition": "superhero.publisher_id is not null",
                                "provenance": ["superhero.publisher_id"],
                            },
                        },
                    ),
                ],
            ),
            collection(
                "hero_catalog_entities",
                purpose="Typed hero, power, and attribute metadata entities.",
                source_tables=["superhero", "superpower", "attribute"],
                transforms=[
                    transform(
                        "hero_catalog_union",
                        "polymorphic_union",
                        module_ref=MODULE_REF,
                        discriminator="entity_type",
                        variants={
                            "hero": {
                                "source_table": "superhero",
                                "fields": {
                                    "entity_id": expr(
                                        "concat('hero:', superhero.id)",
                                        "superhero.id",
                                    ),
                                    "name": field_source("superhero.superhero_name"),
                                    "full_name": field_source("superhero.full_name"),
                                },
                            },
                            "power": {
                                "source_table": "superpower",
                                "fields": {
                                    "entity_id": expr(
                                        "concat('power:', superpower.id)",
                                        "superpower.id",
                                    ),
                                    "name": field_source("superpower.power_name"),
                                },
                            },
                            "attribute": {
                                "source_table": "attribute",
                                "fields": {
                                    "entity_id": expr(
                                        "concat('attribute:', attribute.id)",
                                        "attribute.id",
                                    ),
                                    "name": field_source("attribute.attribute_name"),
                                },
                            },
                        },
                    )
                ],
            ),
        ],
    )
