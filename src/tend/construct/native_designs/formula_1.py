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
            "Represent Formula 1 races with status-keyed results, pit-stop events, "
            "and typed race/driver/constructor entities."
        ),
        collections=[
            collection(
                "race_weekends",
                purpose="Race documents with dynamic result-status buckets.",
                source_tables=["races", "results", "pitStops", "qualifying"],
                transforms=[
                    transform(
                        "results_by_status",
                        "dynamic_key_object",
                        module_ref=MODULE_REF,
                        parent_table="races",
                        child_table="results",
                        join=join("races.raceId", "results.raceId"),
                        target_field="results_by_status",
                        key=expr("results.statusId", "results.statusId"),
                        values={
                            "finishers": expr("count(results.resultId)", "results.resultId"),
                            "points": expr("sum(results.points)", "results.points"),
                            "laps": expr("sum(results.laps)", "results.laps"),
                        },
                    ),
                    transform(
                        "pit_stop_events",
                        "nested_event_stream",
                        module_ref=MODULE_REF,
                        parent_table="races",
                        event_source_table="pitStops",
                        join=join("races.raceId", "pitStops.raceId"),
                        target_field="pit_stops",
                        event_type_field="pitStops.stop",
                        event_time_field="pitStops.time",
                        event_payload={
                            "driver_id": "pitStops.driverId",
                            "lap": "pitStops.lap",
                            "duration": "pitStops.duration",
                            "milliseconds": "pitStops.milliseconds",
                        },
                    ),
                    transform(
                        "race_calendar_tags",
                        "derived_tag_array",
                        module_ref=MODULE_REF,
                        target_field="race_tags",
                        tags={
                            "modern_era": {
                                "condition": "races.year >= 2000",
                                "provenance": ["races.year"],
                            },
                            "has_start_time": {
                                "condition": "races.time is not null",
                                "provenance": ["races.time"],
                            },
                        },
                    ),
                ],
            ),
            collection(
                "f1_entities",
                purpose="Typed drivers, constructors, circuits, and status records.",
                source_tables=["drivers", "constructors", "circuits", "status"],
                transforms=[
                    transform(
                        "f1_entity_union",
                        "polymorphic_union",
                        module_ref=MODULE_REF,
                        discriminator="entity_type",
                        variants={
                            "driver": {
                                "source_table": "drivers",
                                "fields": {
                                    "entity_id": expr(
                                        "concat('driver:', drivers.driverId)",
                                        "drivers.driverId",
                                    ),
                                    "forename": field_source("drivers.forename"),
                                    "surname": field_source("drivers.surname"),
                                    "nationality": field_source("drivers.nationality"),
                                },
                            },
                            "constructor": {
                                "source_table": "constructors",
                                "fields": {
                                    "entity_id": expr(
                                        "concat('constructor:', constructors.constructorId)",
                                        "constructors.constructorId",
                                    ),
                                    "name": field_source("constructors.name"),
                                    "nationality": field_source("constructors.nationality"),
                                },
                            },
                            "circuit": {
                                "source_table": "circuits",
                                "fields": {
                                    "entity_id": expr(
                                        "concat('circuit:', circuits.circuitId)",
                                        "circuits.circuitId",
                                    ),
                                    "name": field_source("circuits.name"),
                                    "country": field_source("circuits.country"),
                                },
                            },
                        },
                    )
                ],
            ),
        ],
    )
