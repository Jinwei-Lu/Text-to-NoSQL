from __future__ import annotations

from collections import defaultdict
from typing import Any

from ...execution import world_signature as compute_world_signature
from ..native_audit import audit_database_structure, validate_structure_gate
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


def materialize_native_dataworld(
    source: Any,
    db_id: str,
    *,
    event_hook: Any = None,
) -> NativeExecutionResult:
    """Build superhero dossiers and universe indexes from real BIRD superhero rows."""
    if db_id != "superhero":
        raise ValueError(f"superhero materializer received db_id={db_id!r}")
    schema = source.schema(db_id)
    conn = source.connection(db_id)

    alignments = _rows(conn, "alignment", ["id"])
    attributes = _rows(conn, "attribute", ["id"])
    colours = _rows(conn, "colour", ["id"])
    genders = _rows(conn, "gender", ["id"])
    publishers = _rows(conn, "publisher", ["id"])
    races = _rows(conn, "race", ["id"])
    heroes = _rows(conn, "superhero", ["id"])
    hero_attributes = _rows(conn, "hero_attribute", ["hero_id", "attribute_id"])
    powers = _rows(conn, "superpower", ["id"])
    hero_powers = _rows(conn, "hero_power", ["hero_id", "power_id"])

    alignments_by_id = _by_id(alignments, "id")
    attributes_by_id = _by_id(attributes, "id")
    colours_by_id = _by_id(colours, "id")
    genders_by_id = _by_id(genders, "id")
    publishers_by_id = _by_id(publishers, "id")
    races_by_id = _by_id(races, "id")
    heroes_by_id = _by_id(heroes, "id")
    powers_by_id = _by_id(powers, "id")
    attrs_by_hero = _group(hero_attributes, "hero_id")
    powers_by_hero = _group(hero_powers, "hero_id")

    hero_docs = [
        _hero_dossier_doc(
            hero,
            db_id=db_id,
            publishers_by_id=publishers_by_id,
            alignments_by_id=alignments_by_id,
            genders_by_id=genders_by_id,
            races_by_id=races_by_id,
            colours_by_id=colours_by_id,
            attributes_by_id=attributes_by_id,
            powers_by_id=powers_by_id,
            hero_attributes=attrs_by_hero.get(hero.get("id"), []),
            hero_powers=powers_by_hero.get(hero.get("id"), []),
        )
        for hero in heroes
    ]
    publisher_docs = _publisher_universe_docs(
        publishers=publishers,
        heroes=heroes,
        hero_docs=hero_docs,
        alignments_by_id=alignments_by_id,
        powers_by_id=powers_by_id,
        powers_by_hero=powers_by_hero,
    )
    ability_docs = _ability_catalog_docs(
        attributes=attributes,
        powers=powers,
        hero_attributes=hero_attributes,
        hero_powers=hero_powers,
        heroes_by_id=heroes_by_id,
        alignments_by_id=alignments_by_id,
    )
    alignment_docs = _alignment_roster_docs(
        alignments=alignments,
        heroes=heroes,
        publishers_by_id=publishers_by_id,
        powers_by_id=powers_by_id,
        powers_by_hero=powers_by_hero,
    )
    data = {
        "hero_dossiers": hero_docs,
        "publisher_universes": publisher_docs,
        "ability_catalog": ability_docs,
        "alignment_rosters": alignment_docs,
    }

    audit = audit_database_structure(db_id, data)
    gate = validate_structure_gate(audit)
    features = _native_features()
    manifest = NativeFeatureManifest(db_id=db_id, features=features)
    native_schema = {
        "db_id": db_id,
        "source_tables": list(schema.tables),
        "collections": {
            "hero_dossiers": {
                "document_count": len(hero_docs),
                "root_entity": "superhero profile dossier",
                "source_tables": [
                    "superhero",
                    "hero_attribute",
                    "attribute",
                    "hero_power",
                    "superpower",
                    "publisher",
                    "alignment",
                    "gender",
                    "race",
                    "colour",
                ],
            },
            "publisher_universes": {
                "document_count": len(publisher_docs),
                "root_entity": "publisher universe with alignment and power buckets",
                "source_tables": ["publisher", "superhero", "alignment", "hero_power", "superpower"],
            },
            "ability_catalog": {
                "document_count": len(ability_docs),
                "root_entity": "typed attribute and power catalog entries",
                "source_tables": ["attribute", "hero_attribute", "superpower", "hero_power", "superhero"],
            },
            "alignment_rosters": {
                "document_count": len(alignment_docs),
                "root_entity": "alignment roster with publisher and power membership buckets",
                "source_tables": ["alignment", "superhero", "publisher", "hero_power", "superpower"],
            },
        },
        "structure_audit": audit.to_dict(),
        "structure_gate": gate.to_dict(),
    }
    provenance = {
        "db_id": db_id,
        "conversion_code_ref": f"{MODULE_REF}.materialize_native_dataworld",
        "entries": {
            feature.id: {
                "source_tables": _source_tables_from_refs(feature.provenance_refs),
                "provenance_refs": list(feature.provenance_refs),
                "field": feature.field,
            }
            for feature in features
        },
    }
    signature = compute_world_signature(data)
    if event_hook is not None:
        event_hook(
            "superhero_native_materialized",
            db_id=db_id,
            collection_count=len(data),
            document_count=sum(len(docs) for docs in data.values()),
            native_feature_count=len(features),
            max_depth=audit.max_depth,
            gate_ok=gate.ok,
            world_signature=signature,
        )
    return NativeExecutionResult(
        data=data,
        schema=native_schema,
        manifest=manifest,
        provenance=provenance,
        world_signature=signature,
        validation=None,
    )


def _rows(conn: Any, table: str, order_by: list[str]) -> list[dict[str, Any]]:
    order_sql = ", ".join(f'"{name}"' for name in order_by)
    cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY {order_sql}')
    columns = [str(item[0]) for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _by_id(rows: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    return {row.get(key): row for row in rows}


def _group(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.get(key)].append(row)
    return dict(grouped)


def _hero_dossier_doc(
    hero: dict[str, Any],
    *,
    db_id: str,
    publishers_by_id: dict[Any, dict[str, Any]],
    alignments_by_id: dict[Any, dict[str, Any]],
    genders_by_id: dict[Any, dict[str, Any]],
    races_by_id: dict[Any, dict[str, Any]],
    colours_by_id: dict[Any, dict[str, Any]],
    attributes_by_id: dict[Any, dict[str, Any]],
    powers_by_id: dict[Any, dict[str, Any]],
    hero_attributes: list[dict[str, Any]],
    hero_powers: list[dict[str, Any]],
) -> dict[str, Any]:
    hero_id = hero.get("id")
    publisher = publishers_by_id.get(hero.get("publisher_id"), {})
    alignment = alignments_by_id.get(hero.get("alignment_id"), {})
    gender = genders_by_id.get(hero.get("gender_id"), {})
    race = races_by_id.get(hero.get("race_id"), {})
    power_items = [_power_ref(powers_by_id.get(link.get("power_id"), {})) for link in hero_powers]
    attributes_by_name = _hero_attributes_by_name(hero_id, hero_attributes, attributes_by_id)
    powers_by_family = _powers_by_family(power_items)
    appearance = _appearance_doc(hero, colours_by_id, gender=gender, race=race)
    profile = _profile_doc(hero)
    universe = _universe_doc(publisher, alignment)
    schema_state = {
        "profile": "present",
        "full_name": _presence_state(hero.get("full_name")),
        "publisher": _presence_state(publisher.get("publisher_name")),
        "alignment": _presence_state(alignment.get("alignment")),
        "powers": "present" if power_items else "missing",
        "attributes": "present" if attributes_by_name else "missing",
        "height_cm": _presence_state(hero.get("height_cm")),
        "weight_kg": _presence_state(hero.get("weight_kg")),
    }
    return {
        "_id": f"hero:{hero_id}",
        "identity": {
            "source_db": db_id,
            "hero_id": hero_id,
            "source_table": "superhero",
            "record_presence_state": "present",
        },
        "profile": profile,
        "universe": universe,
        "appearance": appearance,
        "ability_matrix": {
            "attributes_by_name": attributes_by_name,
            "attribute_score_buckets": _attribute_score_buckets(attributes_by_name),
            "attribute_presence_state": "present" if attributes_by_name else "missing",
        },
        "powers": {
            "powers_by_family": powers_by_family,
            "power_count": len(power_items),
            "power_presence_state": "present" if power_items else "missing",
        },
        "query_views": [
            {
                "view_id": "power_family_rollup",
                "power_families_by_bucket": _power_family_view(powers_by_family),
            },
            {
                "view_id": "attribute_score_rollup",
                "attribute_bands_by_name": _attribute_band_view(attributes_by_name),
            },
        ],
        "schema_state": schema_state,
    }


def _profile_doc(hero: dict[str, Any]) -> dict[str, Any]:
    return {
        "hero_name": _value_state(hero.get("superhero_name")),
        "full_name": _value_state(hero.get("full_name")),
        "body_scale": {
            "height_cm": {
                **_value_state(hero.get("height_cm")),
                "bucket": _height_bucket(hero.get("height_cm")),
            },
            "weight_kg": {
                **_value_state(hero.get("weight_kg")),
                "bucket": _weight_bucket(hero.get("weight_kg")),
            },
        },
    }


def _universe_doc(
    publisher: dict[str, Any],
    alignment: dict[str, Any],
) -> dict[str, Any]:
    publisher_name = publisher.get("publisher_name")
    alignment_name = alignment.get("alignment")
    return {
        "publisher": {
            "publisher_id": publisher.get("id"),
            "name": _value_state(publisher_name),
            "bucket_key": _safe_key(publisher_name, "unknown_publisher"),
        },
        "alignment": {
            "alignment_id": alignment.get("id"),
            "name": _value_state(alignment_name),
            "bucket_key": _safe_key(alignment_name, "unknown_alignment"),
        },
    }


def _appearance_doc(
    hero: dict[str, Any],
    colours_by_id: dict[Any, dict[str, Any]],
    *,
    gender: dict[str, Any],
    race: dict[str, Any],
) -> dict[str, Any]:
    colour_refs = {
        "eye": _colour_role_state(hero.get("eye_colour_id"), colours_by_id),
        "hair": _colour_role_state(hero.get("hair_colour_id"), colours_by_id),
        "skin": _colour_role_state(hero.get("skin_colour_id"), colours_by_id),
    }
    return {
        "gender": {
            "gender_id": gender.get("id"),
            "name": _value_state(gender.get("gender")),
        },
        "race": {
            "race_id": race.get("id"),
            "name": _value_state(race.get("race")),
        },
        "color_refs_by_role": colour_refs,
        "appearance_signature": "|".join(
            str(colour_refs[role]["states"][0]["colour"] or "unknown")
            for role in ("eye", "hair", "skin")
        ),
    }


def _colour_role_state(
    colour_id: Any,
    colours_by_id: dict[Any, dict[str, Any]],
) -> dict[str, Any]:
    colour = colours_by_id.get(colour_id, {})
    colour_name = colour.get("colour")
    return {
        "states": [
            {
                "colour_id": colour_id,
                "colour": colour_name,
                "presence_state": _presence_state(colour_name),
                "source_table": "colour",
            }
        ],
        "presence_state": _presence_state(colour_name),
    }


def _hero_attributes_by_name(
    hero_id: Any,
    hero_attributes: list[dict[str, Any]],
    attributes_by_id: dict[Any, dict[str, Any]],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in hero_attributes:
        attribute = attributes_by_id.get(row.get("attribute_id"), {})
        name = attribute.get("attribute_name")
        key = _safe_key(name, f"attribute_{row.get('attribute_id')}")
        value = row.get("attribute_value")
        out[key] = {
            "attribute_id": row.get("attribute_id"),
            "name": name,
            "observations": [
                {
                    "hero_id": hero_id,
                    "value": value,
                    "presence_state": _presence_state(value),
                    "score": {
                        "value": value,
                        "band": _score_bucket(value),
                        "scale": "0-100",
                    },
                }
            ],
        }
    return out


def _attribute_score_buckets(attributes_by_name: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key, payload in attributes_by_name.items():
        observation = payload["observations"][0]
        bucket = observation["score"]["band"]
        buckets[bucket].append(
            {
                "attribute_key": key,
                "attribute_name": payload["name"],
                "value": observation["value"],
                "presence_state": observation["presence_state"],
            }
        )
    return {key: values for key, values in sorted(buckets.items())}


def _power_ref(power: dict[str, Any]) -> dict[str, Any]:
    name = power.get("power_name")
    return {
        "power_id": power.get("id"),
        "name": name,
        "family": _power_family(name),
        "presence_state": _presence_state(name),
    }


def _powers_by_family(power_items: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in power_items:
        grouped[item["family"]].append(
            {
                "power_id": item["power_id"],
                "name": item["name"],
                "presence_state": item["presence_state"],
            }
        )
    return {
        family: {
            "powers": sorted(values, key=lambda item: (str(item["name"]), item["power_id"])),
            "power_count": len(values),
        }
        for family, values in sorted(grouped.items())
    }


def _power_family_view(powers_by_family: dict[str, Any]) -> dict[str, Any]:
    return {
        family: {
            "power_count": payload["power_count"],
            "powers": [
                {
                    "power_id": item["power_id"],
                    "name": item["name"],
                    "presence_state": item["presence_state"],
                }
                for item in payload["powers"]
            ],
        }
        for family, payload in powers_by_family.items()
    }


def _attribute_band_view(attributes_by_name: dict[str, Any]) -> dict[str, Any]:
    return {
        key: {
            "band": payload["observations"][0]["score"]["band"],
            "value": payload["observations"][0]["value"],
            "presence_state": payload["observations"][0]["presence_state"],
        }
        for key, payload in sorted(attributes_by_name.items())
    }


def _publisher_universe_docs(
    *,
    publishers: list[dict[str, Any]],
    heroes: list[dict[str, Any]],
    hero_docs: list[dict[str, Any]],
    alignments_by_id: dict[Any, dict[str, Any]],
    powers_by_id: dict[Any, dict[str, Any]],
    powers_by_hero: dict[Any, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    heroes_by_publisher = _group(heroes, "publisher_id")
    docs: list[dict[str, Any]] = []
    for publisher in publishers:
        publisher_id = publisher.get("id")
        members = heroes_by_publisher.get(publisher_id, [])
        alignment_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        power_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for hero in members:
            alignment = alignments_by_id.get(hero.get("alignment_id"), {})
            alignment_key = _safe_key(alignment.get("alignment"), "unknown_alignment")
            hero_ref = _hero_ref(hero)
            alignment_buckets[alignment_key].append(hero_ref)
            for power_link in powers_by_hero.get(hero.get("id"), []):
                power = powers_by_id.get(power_link.get("power_id"), {})
                family = _power_family(power.get("power_name"))
                power_buckets[family].append(
                    {
                        **hero_ref,
                        "power_id": power.get("id"),
                        "power_name": power.get("power_name"),
                        "presence_state": _presence_state(power.get("power_name")),
                    }
                )
        docs.append(
            {
                "_id": f"publisher:{publisher_id}",
                "publisher": {
                    "publisher_id": publisher_id,
                    "name": _value_state(publisher.get("publisher_name")),
                    "hero_count": len(members),
                },
                "alignment_buckets_by_name": {
                    key: {"heroes": sorted(values, key=lambda item: item["hero_id"])}
                    for key, values in sorted(alignment_buckets.items())
                },
                "power_families_by_bucket": {
                    key: {"heroes": sorted(values, key=lambda item: (item["hero_id"], item["power_id"]))}
                    for key, values in sorted(power_buckets.items())
                },
                "query_views": [
                    {
                        "view_id": "publisher_alignment_power_matrix",
                        "alignment_power_matrix_by_alignment": _publisher_alignment_power_matrix(
                            members, alignments_by_id, powers_by_id, powers_by_hero
                        ),
                    }
                ],
                "schema_state": {
                    "publisher_name": _presence_state(publisher.get("publisher_name")),
                    "heroes": "present" if members else "missing",
                    "sample_hero_dossiers": "present" if hero_docs else "missing",
                },
            }
        )
    return docs


def _publisher_alignment_power_matrix(
    heroes: list[dict[str, Any]],
    alignments_by_id: dict[Any, dict[str, Any]],
    powers_by_id: dict[Any, dict[str, Any]],
    powers_by_hero: dict[Any, list[dict[str, Any]]],
) -> dict[str, Any]:
    matrix: dict[str, dict[str, Any]] = {}
    for hero in heroes:
        alignment = alignments_by_id.get(hero.get("alignment_id"), {})
        alignment_key = _safe_key(alignment.get("alignment"), "unknown_alignment")
        payload = matrix.setdefault(
            alignment_key,
            {"hero_count": 0, "power_families_by_bucket": defaultdict(list)},
        )
        payload["hero_count"] += 1
        for link in powers_by_hero.get(hero.get("id"), []):
            power = powers_by_id.get(link.get("power_id"), {})
            family = _power_family(power.get("power_name"))
            payload["power_families_by_bucket"][family].append(
                {
                    "hero_id": hero.get("id"),
                    "hero_name": hero.get("superhero_name"),
                    "power_id": power.get("id"),
                    "power_name": power.get("power_name"),
                }
            )
    return {
        key: {
            "hero_count": payload["hero_count"],
            "power_families_by_bucket": {
                family: {"members": values}
                for family, values in sorted(payload["power_families_by_bucket"].items())
            },
        }
        for key, payload in sorted(matrix.items())
    }


def _ability_catalog_docs(
    *,
    attributes: list[dict[str, Any]],
    powers: list[dict[str, Any]],
    hero_attributes: list[dict[str, Any]],
    hero_powers: list[dict[str, Any]],
    heroes_by_id: dict[Any, dict[str, Any]],
    alignments_by_id: dict[Any, dict[str, Any]],
) -> list[dict[str, Any]]:
    attrs_by_attribute = _group(hero_attributes, "attribute_id")
    powers_by_power = _group(hero_powers, "power_id")
    docs: list[dict[str, Any]] = []
    for attribute in attributes:
        rows = attrs_by_attribute.get(attribute.get("id"), [])
        values_by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            hero = heroes_by_id.get(row.get("hero_id"), {})
            values_by_bucket[_score_bucket(row.get("attribute_value"))].append(
                {
                    **_hero_ref(hero),
                    "value": row.get("attribute_value"),
                    "presence_state": _presence_state(row.get("attribute_value")),
                }
            )
        docs.append(
            {
                "_id": f"attribute:{attribute.get('id')}",
                "catalog_type": "attribute",
                "attribute": {
                    "attribute_id": attribute.get("id"),
                    "name": _value_state(attribute.get("attribute_name")),
                },
                "values_by_bucket": {
                    key: {"heroes": sorted(values, key=lambda item: item["hero_id"])}
                    for key, values in sorted(values_by_bucket.items())
                },
                "schema_state": {
                    "catalog_entry": "present",
                    "hero_values": "present" if rows else "missing",
                },
            }
        )
    for power in powers:
        rows = powers_by_power.get(power.get("id"), [])
        heroes_by_alignment: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            hero = heroes_by_id.get(row.get("hero_id"), {})
            alignment = alignments_by_id.get(hero.get("alignment_id"), {})
            alignment_key = _safe_key(alignment.get("alignment"), "unknown_alignment")
            heroes_by_alignment[alignment_key].append(
                {
                    **_hero_ref(hero),
                    "alignment": alignment.get("alignment"),
                    "presence_state": _presence_state(hero.get("id")),
                }
            )
        docs.append(
            {
                "_id": f"power:{power.get('id')}",
                "catalog_type": "power",
                "power": {
                    "power_id": power.get("id"),
                    "name": _value_state(power.get("power_name")),
                    "family": _power_family(power.get("power_name")),
                },
                "heroes_by_alignment": {
                    key: {"heroes": sorted(values, key=lambda item: item["hero_id"])}
                    for key, values in sorted(heroes_by_alignment.items())
                },
                "query_views": [
                    {
                        "view_id": "power_alignment_distribution",
                        "alignment_members_by_name": {
                            key: {
                                "hero_count": len(values),
                                "heroes": sorted(values, key=lambda item: item["hero_id"]),
                            }
                            for key, values in sorted(heroes_by_alignment.items())
                        },
                    }
                ],
                "schema_state": {
                    "catalog_entry": "present",
                    "hero_memberships": "present" if rows else "missing",
                },
            }
        )
    return docs


def _alignment_roster_docs(
    *,
    alignments: list[dict[str, Any]],
    heroes: list[dict[str, Any]],
    publishers_by_id: dict[Any, dict[str, Any]],
    powers_by_id: dict[Any, dict[str, Any]],
    powers_by_hero: dict[Any, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    heroes_by_alignment = _group(heroes, "alignment_id")
    docs: list[dict[str, Any]] = []
    for alignment in alignments:
        members = heroes_by_alignment.get(alignment.get("id"), [])
        publishers_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        power_families: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for hero in members:
            publisher = publishers_by_id.get(hero.get("publisher_id"), {})
            publisher_key = _safe_key(publisher.get("publisher_name"), "unknown_publisher")
            hero_ref = _hero_ref(hero)
            publishers_by_name[publisher_key].append(
                {
                    **hero_ref,
                    "publisher_id": publisher.get("id"),
                    "publisher_name": publisher.get("publisher_name"),
                    "presence_state": _presence_state(publisher.get("publisher_name")),
                }
            )
            for link in powers_by_hero.get(hero.get("id"), []):
                power = powers_by_id.get(link.get("power_id"), {})
                family = _power_family(power.get("power_name"))
                power_families[family].append(
                    {
                        **hero_ref,
                        "power_id": power.get("id"),
                        "power_name": power.get("power_name"),
                    }
                )
        docs.append(
            {
                "_id": f"alignment:{alignment.get('id')}",
                "alignment": {
                    "alignment_id": alignment.get("id"),
                    "name": _value_state(alignment.get("alignment")),
                },
                "publishers_by_name": {
                    key: {"heroes": sorted(values, key=lambda item: item["hero_id"])}
                    for key, values in sorted(publishers_by_name.items())
                },
                "power_families_by_bucket": {
                    key: {"members": sorted(values, key=lambda item: (item["hero_id"], item["power_id"]))}
                    for key, values in sorted(power_families.items())
                },
                "schema_state": {
                    "alignment": _presence_state(alignment.get("alignment")),
                    "heroes": "present" if members else "missing",
                },
            }
        )
    return docs


def _hero_ref(hero: dict[str, Any]) -> dict[str, Any]:
    return {
        "hero_id": hero.get("id"),
        "hero_name": hero.get("superhero_name"),
        "full_name_presence_state": _presence_state(hero.get("full_name")),
    }


def _value_state(value: Any) -> dict[str, Any]:
    return {
        "value": value,
        "presence_state": _presence_state(value),
    }


def _presence_state(value: Any) -> str:
    if value is None:
        return "null"
    if value == "" or value == [] or value == {}:
        return "empty"
    return "present"


def _safe_key(value: Any, fallback: str) -> str:
    if value is None or value == "":
        return fallback
    text = str(value).strip().lower()
    for old, new in [
        (" ", "_"),
        ("/", "_"),
        (".", "_"),
        ("$", "s"),
        ("-", "_"),
        ("'", ""),
        ("&", "and"),
    ]:
        text = text.replace(old, new)
    return text or fallback


def _height_bucket(value: Any) -> str:
    if value is None:
        return "unknown_height"
    if value < 150:
        return "short"
    if value <= 190:
        return "human_scale"
    if value <= 220:
        return "towering"
    return "giant_scale"


def _weight_bucket(value: Any) -> str:
    if value is None:
        return "unknown_weight"
    if value < 60:
        return "lightweight"
    if value <= 100:
        return "middleweight"
    if value <= 180:
        return "heavyweight"
    return "superheavy"


def _score_bucket(value: Any) -> str:
    if value is None:
        return "unknown"
    if value >= 90:
        return "legendary"
    if value >= 70:
        return "elite"
    if value >= 40:
        return "capable"
    return "limited"


def _power_family(name: Any) -> str:
    text = str(name or "").lower()
    if any(token in text for token in ("flight", "speed", "teleport", "travel", "movement")):
        return "mobility"
    if any(token in text for token in ("strength", "durability", "stamina", "healing", "invulnerability")):
        return "physical"
    if any(token in text for token in ("telepathy", "telekinesis", "mind", "intelligence", "awareness")):
        return "mental"
    if any(token in text for token in ("energy", "radiation", "fire", "ice", "heat", "light", "electric")):
        return "energy"
    if any(token in text for token in ("magic", "time", "dimensional", "reality", "immortal")):
        return "cosmic"
    if any(token in text for token in ("weapon", "marksmanship", "combat", "stealth", "tracking")):
        return "tactical"
    return "specialized"


def _source_tables_from_refs(refs: list[str]) -> list[str]:
    return sorted({ref.split(".", 1)[0] for ref in refs if "." in ref})


def _native_features() -> list[NativeFeature]:
    return [
        NativeFeature(
            id="hero_dossiers.attribute_matrix",
            type="dynamic_key_object",
            collection="hero_dossiers",
            field="ability_matrix.attributes_by_name",
            query_patterns=["hero_attribute_score_matrix"],
            required_constructs=["$objectToArray", "$unwind", "$group", "$avg", "$max"],
            provenance_refs=["hero_attribute.attribute_value", "attribute.attribute_name", "superhero.id"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "hero_attribute_score_matrix",
                        "intent": "compare hero attribute observations across dynamic attribute-name keys",
                        "pipeline": [
                            {
                                "$project": {
                                    "hero_id": "$identity.hero_id",
                                    "hero_name": "$profile.hero_name.value",
                                    "attributes": {"$objectToArray": "$ability_matrix.attributes_by_name"},
                                }
                            },
                            {"$unwind": "$attributes"},
                            {"$unwind": "$attributes.v.observations"},
                            {
                                "$group": {
                                    "_id": "$attributes.k",
                                    "avg_score": {"$avg": "$attributes.v.observations.value"},
                                    "max_score": {"$max": "$attributes.v.observations.value"},
                                    "hero_count": {"$sum": 1},
                                }
                            },
                            {"$sort": {"max_score": -1, "_id": 1}},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$avg", "$max"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="hero_dossiers.power_family_views",
            type="array_object_dynamic_key",
            collection="hero_dossiers",
            field="query_views.power_families_by_bucket",
            query_patterns=["hero_power_family_distribution"],
            required_constructs=["$unwind", "$objectToArray", "$group", "$sum"],
            provenance_refs=["hero_power.power_id", "superpower.power_name", "superhero.id"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "hero_power_family_distribution",
                        "intent": "traverse per-hero query views and dynamic power-family buckets",
                        "pipeline": [
                            {"$unwind": "$query_views"},
                            {
                                "$project": {
                                    "hero_id": "$identity.hero_id",
                                    "families": {"$objectToArray": "$query_views.power_families_by_bucket"},
                                }
                            },
                            {"$unwind": "$families"},
                            {
                                "$group": {
                                    "_id": "$families.k",
                                    "hero_count": {"$sum": 1},
                                    "power_mentions": {"$sum": "$families.v.power_count"},
                                }
                            },
                            {"$sort": {"hero_count": -1, "_id": 1}},
                        ],
                        "mongo_native_constructs": ["$unwind", "$objectToArray", "$group", "$sum"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="publisher_universes.alignment_power_matrix",
            type="dynamic_key_object",
            collection="publisher_universes",
            field="query_views.alignment_power_matrix_by_alignment",
            query_patterns=["publisher_alignment_power_matrix"],
            required_constructs=["$unwind", "$objectToArray", "$group", "$size"],
            provenance_refs=["publisher.publisher_name", "alignment.alignment", "hero_power.power_id"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "publisher_alignment_power_matrix",
                        "intent": "summarize publisher universes by alignment keys and nested power-family buckets",
                        "pipeline": [
                            {"$unwind": "$query_views"},
                            {
                                "$project": {
                                    "publisher": "$publisher.name.value",
                                    "alignments": {
                                        "$objectToArray": "$query_views.alignment_power_matrix_by_alignment"
                                    },
                                }
                            },
                            {"$unwind": "$alignments"},
                            {
                                "$project": {
                                    "publisher": 1,
                                    "alignment": "$alignments.k",
                                    "hero_count": "$alignments.v.hero_count",
                                    "families": {
                                        "$objectToArray": "$alignments.v.power_families_by_bucket"
                                    },
                                }
                            },
                            {"$unwind": "$families"},
                            {
                                "$group": {
                                    "_id": {
                                        "publisher": "$publisher",
                                        "alignment": "$alignment",
                                        "family": "$families.k",
                                    },
                                    "member_count": {"$sum": {"$size": "$families.v.members"}},
                                    "hero_count": {"$max": "$hero_count"},
                                }
                            },
                            {"$sort": {"member_count": -1, "_id.publisher": 1}},
                            {"$limit": 60},
                        ],
                        "mongo_native_constructs": ["$unwind", "$objectToArray", "$group", "$size", "$max"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="ability_catalog.power_to_hero_index",
            type="dynamic_key_object",
            collection="ability_catalog",
            field="heroes_by_alignment",
            query_patterns=["power_alignment_hero_index"],
            required_constructs=["$match", "$objectToArray", "$unwind", "$group", "$sum"],
            provenance_refs=["superpower.power_name", "hero_power.hero_id", "alignment.alignment"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "power_alignment_hero_index",
                        "intent": "read power catalog entries through alignment-keyed hero membership bags",
                        "pipeline": [
                            {"$match": {"catalog_type": "power"}},
                            {
                                "$project": {
                                    "power_name": "$power.name.value",
                                    "family": "$power.family",
                                    "alignments": {"$objectToArray": "$heroes_by_alignment"},
                                }
                            },
                            {"$unwind": "$alignments"},
                            {"$unwind": "$alignments.v.heroes"},
                            {
                                "$group": {
                                    "_id": {
                                        "family": "$family",
                                        "alignment": "$alignments.k",
                                    },
                                    "power_count": {"$addToSet": "$power_name"},
                                    "hero_mentions": {"$sum": 1},
                                }
                            },
                            {
                                "$project": {
                                    "_id": 0,
                                    "family": "$_id.family",
                                    "alignment": "$_id.alignment",
                                    "distinct_powers": {"$size": "$power_count"},
                                    "hero_mentions": 1,
                                }
                            },
                            {"$sort": {"hero_mentions": -1, "family": 1}},
                        ],
                        "mongo_native_constructs": ["$match", "$objectToArray", "$unwind", "$group", "$sum", "$size"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="hero_dossiers.profile_presence_states",
            type="missing_vs_present",
            collection="hero_dossiers",
            field="schema_state.attributes",
            query_patterns=["missing_vs_present"],
            required_constructs=["$ifNull", "$cond"],
            provenance_refs=["superhero.full_name", "superhero.height_cm", "hero_power.power_id"],
        ),
    ]
