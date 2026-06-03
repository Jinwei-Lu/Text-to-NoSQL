from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ...execution import world_signature as compute_world_signature
from ..native_audit import audit_database_structure
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


def materialize_native_dataworld(
    source: Any,
    db_id: str,
    *,
    event_hook: Any = None,
) -> NativeExecutionResult:
    """Build Formula 1 race-weekend documents from the actual SQLite schema."""
    if db_id != "formula_1":
        raise ValueError(f"formula_1 materializer received db_id={db_id!r}")
    schema = source.schema(db_id)
    conn = source.connection(db_id)

    circuits = _by_id(_rows(conn, "circuits", ["circuitId"]), "circuitId")
    constructors = _by_id(_rows(conn, "constructors", ["constructorId"]), "constructorId")
    drivers = _by_id(_rows(conn, "drivers", ["driverId"]), "driverId")
    statuses = _by_id(_rows(conn, "status", ["statusId"]), "statusId")
    races = _rows(conn, "races", ["year", "round", "raceId"])
    results_by_race = _group(_rows(conn, "results", ["raceId", "positionOrder", "resultId"]), "raceId")
    qualifying_by_race = _group(_rows(conn, "qualifying", ["raceId", "position", "qualifyId"]), "raceId")
    pit_stops_by_race_driver = _group2(
        _rows(conn, "pitStops", ["raceId", "driverId", "stop"]),
        "raceId",
        "driverId",
    )
    lap_times_by_race_driver = _group2(
        _rows(conn, "lapTimes", ["raceId", "driverId", "lap"]),
        "raceId",
        "driverId",
    )
    lap_times_by_race = _group(_rows(conn, "lapTimes", ["raceId", "lap", "position"]), "raceId")
    driver_standings_by_race = _group(
        _rows(conn, "driverStandings", ["raceId", "position", "driverStandingsId"]),
        "raceId",
    )
    constructor_standings_by_race = _group(
        _rows(conn, "constructorStandings", ["raceId", "position", "constructorStandingsId"]),
        "raceId",
    )
    constructor_results_by_race = _group(
        _rows(conn, "constructorResults", ["raceId", "constructorId", "constructorResultsId"]),
        "raceId",
    )

    race_docs = [
        _race_weekend_doc(
            race,
            circuits=circuits,
            constructors=constructors,
            drivers=drivers,
            statuses=statuses,
            results=results_by_race.get(race["raceId"], []),
            qualifying=qualifying_by_race.get(race["raceId"], []),
            pit_stops_by_race_driver=pit_stops_by_race_driver.get(race["raceId"], {}),
            lap_times_by_race_driver=lap_times_by_race_driver.get(race["raceId"], {}),
            lap_times=lap_times_by_race.get(race["raceId"], []),
            driver_standings=driver_standings_by_race.get(race["raceId"], []),
            constructor_standings=constructor_standings_by_race.get(race["raceId"], []),
            constructor_results=constructor_results_by_race.get(race["raceId"], []),
        )
        for race in races
    ]
    actor_docs = _actor_profile_docs(
        drivers=drivers,
        constructors=constructors,
        circuits=circuits,
        races=races,
        results_by_race=results_by_race,
    )
    data = {
        "f1_actor_profiles": actor_docs,
        "race_weekends_v2": race_docs,
    }
    audit = audit_database_structure(db_id, data)
    features = _native_features()
    manifest = NativeFeatureManifest(db_id=db_id, features=features)
    native_schema = {
        "db_id": db_id,
        "source_tables": list(schema.tables),
        "collections": {
            "race_weekends_v2": {
                "document_count": len(race_docs),
                "root_entity": "race weekend",
                "source_tables": [
                    "races",
                    "circuits",
                    "results",
                    "qualifying",
                    "lapTimes",
                    "pitStops",
                    "driverStandings",
                    "constructorStandings",
                ],
            },
            "f1_actor_profiles": {
                "document_count": len(actor_docs),
                "root_entity": "driver/constructor/circuit profile",
                "source_tables": ["drivers", "constructors", "circuits", "results", "races"],
            },
        },
        "structure_audit": audit.to_dict(),
    }
    provenance = {
        feature.id: {
            "module": MODULE_REF,
            "source_tables": feature.provenance_refs,
            "field": feature.field,
        }
        for feature in features
    }
    signature = compute_world_signature(data)
    if event_hook is not None:
        event_hook(
            "direct_materialized",
            db_id=db_id,
            collection_count=len(data),
            document_count=sum(len(docs) for docs in data.values()),
            native_feature_count=len(features),
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
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _by_id(rows: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    return {row[key]: row for row in rows}


def _group(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    out: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[row[key]].append(row)
    return dict(out)


def _group2(
    rows: list[dict[str, Any]],
    left_key: str,
    right_key: str,
) -> dict[Any, dict[Any, list[dict[str, Any]]]]:
    out: dict[Any, dict[Any, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        out[row[left_key]][row[right_key]].append(row)
    return {left: dict(right) for left, right in out.items()}


def _race_weekend_doc(
    race: dict[str, Any],
    *,
    circuits: dict[Any, dict[str, Any]],
    constructors: dict[Any, dict[str, Any]],
    drivers: dict[Any, dict[str, Any]],
    statuses: dict[Any, dict[str, Any]],
    results: list[dict[str, Any]],
    qualifying: list[dict[str, Any]],
    pit_stops_by_race_driver: dict[Any, list[dict[str, Any]]],
    lap_times_by_race_driver: dict[Any, list[dict[str, Any]]],
    lap_times: list[dict[str, Any]],
    driver_standings: list[dict[str, Any]],
    constructor_standings: list[dict[str, Any]],
    constructor_results: list[dict[str, Any]],
) -> dict[str, Any]:
    circuit = circuits.get(race["circuitId"], {})
    result_entries = [
        _race_result_entry(
            result,
            driver=drivers.get(result["driverId"], {}),
            constructor=constructors.get(result["constructorId"], {}),
            status=statuses.get(result["statusId"], {}),
            pit_stops=pit_stops_by_race_driver.get(result["driverId"], []),
            lap_times=lap_times_by_race_driver.get(result["driverId"], []),
        )
        for result in results
    ]
    return {
        "_id": f"race:{race['raceId']}",
        "race_id": race["raceId"],
        "calendar": {
            "season_year": race["year"],
            "round": race["round"],
            "date": race["date"],
            "time": {"value": race.get("time"), "state": _presence(race.get("time"))},
            "race_name": race["name"],
            "url": race.get("url"),
        },
        "circuit": _circuit_snapshot(circuit),
        "sessions": {
            "qualifying": {
                "schema_state": _presence(qualifying),
                "entries": [
                    _qualifying_entry(
                        item,
                        driver=drivers.get(item["driverId"], {}),
                        constructor=constructors.get(item["constructorId"], {}),
                    )
                    for item in qualifying
                ],
                "elimination_windows": _qualifying_elimination_windows(qualifying, drivers),
            },
            "race": {
                "schema_state": _presence(results),
                "entries": result_entries,
                "results_by_status": _results_by_status(result_entries),
                "laps_by_number": _laps_by_number(
                    lap_times,
                    result_entries_by_driver={entry["driver"]["driver_id"]: entry for entry in result_entries},
                ),
                "constructor_points_by_team": _constructor_points_by_team(
                    constructor_results,
                    constructors,
                ),
            },
        },
        "standings_after": {
            "drivers_by_position": _driver_standings_by_position(driver_standings, drivers),
            "constructors_by_position": _constructor_standings_by_position(
                constructor_standings,
                constructors,
            ),
        },
        "schema_state": {
            "race_time": _presence(race.get("time")),
            "qualifying": _presence(qualifying),
            "race_results": _presence(results),
            "pit_stops": _presence([stop for stops in pit_stops_by_race_driver.values() for stop in stops]),
            "lap_times": _presence(lap_times),
            "driver_standings": _presence(driver_standings),
            "external_weather_feed": "missing",
        },
        "_provenance": {
            "source_tables": [
                "races",
                "circuits",
                "qualifying",
                "results",
                "lapTimes",
                "pitStops",
                "driverStandings",
                "constructorStandings",
            ],
            "source_keys": {"raceId": race["raceId"], "circuitId": race["circuitId"]},
        },
    }


def _race_result_entry(
    result: dict[str, Any],
    *,
    driver: dict[str, Any],
    constructor: dict[str, Any],
    status: dict[str, Any],
    pit_stops: list[dict[str, Any]],
    lap_times: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "result_id": result["resultId"],
        "driver": _driver_snapshot(driver),
        "constructor": _constructor_snapshot(constructor),
        "grid": result.get("grid"),
        "finish": {
            "position": result.get("position"),
            "position_text": result.get("positionText"),
            "position_order": result.get("positionOrder"),
            "points": result.get("points"),
            "laps_completed": result.get("laps"),
            "classified_state": _status_class(result, status),
            "status": status.get("status") or f"status:{result.get('statusId')}",
        },
        "timing": {
            "total_time": {"value": result.get("time"), "state": _presence(result.get("time"))},
            "milliseconds": result.get("milliseconds"),
            "fastest_lap": {
                "lap": result.get("fastestLap"),
                "rank": result.get("rank"),
                "time": result.get("fastestLapTime"),
                "speed": result.get("fastestLapSpeed"),
            },
        },
        "pit_stops": [_pit_stop_event(item) for item in pit_stops],
        "pace_profile": {
            "schema_state": {
                "lap_times": _presence(lap_times),
                "pit_stops": _presence(pit_stops),
                "sector_splits": "missing",
            },
            "laps_by_number": _driver_laps_by_number(lap_times),
        },
    }


def _qualifying_entry(
    item: dict[str, Any],
    *,
    driver: dict[str, Any],
    constructor: dict[str, Any],
) -> dict[str, Any]:
    return {
        "qualify_id": item["qualifyId"],
        "driver": _driver_snapshot(driver),
        "constructor": _constructor_snapshot(constructor),
        "car_number": item.get("number"),
        "position": item.get("position"),
        "times_by_phase": {
            phase: {"value": item.get(phase), "state": _presence(item.get(phase))}
            for phase in ("q1", "q2", "q3")
        },
    }


def _qualifying_elimination_windows(
    qualifying: list[dict[str, Any]],
    drivers: dict[Any, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    ordered = sorted(
        qualifying,
        key=lambda row: (_presence(row.get("q1")) != "present", row.get("q1") or ""),
        reverse=True,
    )
    return {
        "q1_slowest_five": {
            "schema_state": _presence(ordered),
            "entries": [
                {
                    "driver": _driver_snapshot(drivers.get(item["driverId"], {})),
                    "position": item.get("position"),
                    "q1": item.get("q1"),
                }
                for item in ordered[:5]
            ],
        }
    }


def _results_by_status(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        buckets[_safe_key(entry["finish"]["status"])].append(entry)
    out: dict[str, dict[str, Any]] = {}
    for status, items in sorted(buckets.items()):
        out[status] = {
            "schema_state": _presence(items),
            "count": len(items),
            "points_total": round(sum(float(item["finish"].get("points") or 0.0) for item in items), 4),
            "laps_total": sum(int(item["finish"].get("laps_completed") or 0) for item in items),
            "entries": [
                {
                    "result_id": item["result_id"],
                    "driver": item["driver"],
                    "constructor": item["constructor"],
                    "finish": item["finish"],
                }
                for item in items
            ],
        }
    return out


def _laps_by_number(
    lap_times: list[dict[str, Any]],
    *,
    result_entries_by_driver: dict[Any, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for lap in lap_times:
        grouped[lap["lap"]].append(lap)
    out: dict[str, dict[str, Any]] = {}
    for lap_number, rows in sorted(grouped.items()):
        ordered = sorted(rows, key=lambda row: (row.get("position") is None, row.get("position") or 999))
        out[str(lap_number)] = {
            "schema_state": _presence(ordered),
            "running_order": [
                _lap_running_order_item(row, result_entries_by_driver.get(row["driverId"]))
                for row in ordered
            ],
        }
    return out


def _lap_running_order_item(
    row: dict[str, Any],
    entry: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "driver": (entry or {}).get("driver", {"driver_id": row["driverId"]}),
        "constructor": (entry or {}).get("constructor", {}),
        "position": row.get("position"),
        "timing": {
            "lap_time": row.get("time"),
            "milliseconds": row.get("milliseconds"),
            "time_state": _presence(row.get("time")),
        },
    }


def _driver_laps_by_number(lap_times: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item["lap"]): {
            "position": item.get("position"),
            "time": item.get("time"),
            "milliseconds": item.get("milliseconds"),
            "state": _presence(item.get("time")),
        }
        for item in lap_times
    }


def _pit_stop_event(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "stop": item.get("stop"),
        "lap": item.get("lap"),
        "time": {"value": item.get("time"), "state": _presence(item.get("time"))},
        "duration": {
            "raw": item.get("duration"),
            "milliseconds": item.get("milliseconds"),
            "state": _presence(item.get("duration")),
        },
    }


def _constructor_points_by_team(
    rows: list[dict[str, Any]],
    constructors: dict[Any, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        constructor = constructors.get(row["constructorId"], {})
        key = _safe_key(constructor.get("constructorRef") or row["constructorId"])
        out[key] = {
            "constructor": _constructor_snapshot(constructor),
            "points": row.get("points"),
            "status": {"value": row.get("status"), "state": _presence(row.get("status"))},
        }
    return out


def _driver_standings_by_position(
    rows: list[dict[str, Any]],
    drivers: dict[Any, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        str(row["position"]): {
            "driver": _driver_snapshot(drivers.get(row["driverId"], {})),
            "points": row.get("points"),
            "position_text": row.get("positionText"),
            "wins": row.get("wins"),
        }
        for row in rows
        if row.get("position") is not None
    }


def _constructor_standings_by_position(
    rows: list[dict[str, Any]],
    constructors: dict[Any, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        str(row["position"]): {
            "constructor": _constructor_snapshot(constructors.get(row["constructorId"], {})),
            "points": row.get("points"),
            "position_text": row.get("positionText"),
            "wins": row.get("wins"),
        }
        for row in rows
        if row.get("position") is not None
    }


def _actor_profile_docs(
    *,
    drivers: dict[Any, dict[str, Any]],
    constructors: dict[Any, dict[str, Any]],
    circuits: dict[Any, dict[str, Any]],
    races: list[dict[str, Any]],
    results_by_race: dict[Any, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    races_by_year = _group(races, "year")
    driver_results_by_year: dict[Any, dict[Any, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    constructor_results_by_year: dict[Any, dict[Any, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    race_by_id = {race["raceId"]: race for race in races}
    for race_id, results in results_by_race.items():
        year = race_by_id.get(race_id, {}).get("year")
        if year is None:
            continue
        for result in results:
            driver_results_by_year[result["driverId"]][year].append(result)
            constructor_results_by_year[result["constructorId"]][year].append(result)

    docs: list[dict[str, Any]] = []
    for driver_id, driver in sorted(drivers.items()):
        by_year = driver_results_by_year.get(driver_id, {})
        docs.append(
            {
                "_id": f"driver:{driver_id}",
                "entity_type": "driver",
                "driver": _driver_snapshot(driver),
                "career_by_year": {
                    str(year): {
                        "race_count": len(rows),
                        "points": round(sum(float(row.get("points") or 0.0) for row in rows), 4),
                        "finishes_by_position_order": {
                            str(row["positionOrder"]): {
                                "race_id": row["raceId"],
                                "points": row.get("points"),
                            }
                            for row in rows
                            if row.get("positionOrder") is not None
                        },
                    }
                    for year, rows in sorted(by_year.items())
                },
                "schema_state": {"career_results": _presence([row for rows in by_year.values() for row in rows])},
            }
        )
    for constructor_id, constructor in sorted(constructors.items()):
        by_year = constructor_results_by_year.get(constructor_id, {})
        docs.append(
            {
                "_id": f"constructor:{constructor_id}",
                "entity_type": "constructor",
                "constructor": _constructor_snapshot(constructor),
                "seasons_by_year": {
                    str(year): {
                        "race_count": len(rows),
                        "points": round(sum(float(row.get("points") or 0.0) for row in rows), 4),
                    }
                    for year, rows in sorted(by_year.items())
                },
                "schema_state": {"season_results": _presence([row for rows in by_year.values() for row in rows])},
            }
        )
    for circuit_id, circuit in sorted(circuits.items()):
        race_rows = [race for races_in_year in races_by_year.values() for race in races_in_year if race["circuitId"] == circuit_id]
        docs.append(
            {
                "_id": f"circuit:{circuit_id}",
                "entity_type": "circuit",
                "circuit": _circuit_snapshot(circuit),
                "races_by_year": {
                    str(year): [
                        {
                            "race_id": race["raceId"],
                            "round": race.get("round"),
                            "name": race.get("name"),
                            "date": race.get("date"),
                        }
                        for race in race_rows
                        if race["year"] == year
                    ]
                    for year in sorted({race["year"] for race in race_rows})
                },
                "schema_state": {"race_history": _presence(race_rows), "circuit_altitude": _presence(circuit.get("alt"))},
            }
        )
    return docs


def _driver_snapshot(driver: dict[str, Any]) -> dict[str, Any]:
    return {
        "driver_id": driver.get("driverId"),
        "ref": driver.get("driverRef"),
        "code": driver.get("code"),
        "number": driver.get("number"),
        "name": {
            "forename": driver.get("forename"),
            "surname": driver.get("surname"),
            "display": " ".join(part for part in [driver.get("forename"), driver.get("surname")] if part),
        },
        "nationality": driver.get("nationality"),
        "dob": {"value": driver.get("dob"), "state": _presence(driver.get("dob"))},
    }


def _constructor_snapshot(constructor: dict[str, Any]) -> dict[str, Any]:
    return {
        "constructor_id": constructor.get("constructorId"),
        "ref": constructor.get("constructorRef"),
        "name": constructor.get("name"),
        "nationality": constructor.get("nationality"),
    }


def _circuit_snapshot(circuit: dict[str, Any]) -> dict[str, Any]:
    return {
        "circuit_id": circuit.get("circuitId"),
        "ref": circuit.get("circuitRef"),
        "name": circuit.get("name"),
        "location": {
            "city": circuit.get("location"),
            "country": circuit.get("country"),
            "coordinates": {
                "lat": circuit.get("lat"),
                "lng": circuit.get("lng"),
                "state": "present" if circuit.get("lat") is not None and circuit.get("lng") is not None else "null",
            },
            "altitude": {"value": circuit.get("alt"), "state": _presence(circuit.get("alt"))},
        },
        "country": circuit.get("country"),
        "url": circuit.get("url"),
    }


def _status_class(result: dict[str, Any], status: dict[str, Any]) -> str:
    label = str(status.get("status") or "")
    if label == "Finished" or label.startswith("+"):
        return "classified"
    if result.get("positionOrder") is not None and result.get("laps"):
        return "classified_nonfinish"
    return "retired"


def _presence(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return "empty" if value == "" else "present"
    if isinstance(value, (list, tuple, dict, set)):
        return "present" if len(value) > 0 else "empty"
    return "present"


def _safe_key(value: Any) -> str:
    text = str(value if value is not None else "unknown")
    text = text.replace(".", "_dot_").replace("$", "S")
    text = re.sub(r"\s+", " ", text).strip()
    return text or "unknown"


def _native_features() -> list[NativeFeature]:
    return [
        NativeFeature(
            id="race_weekends_v2.results_by_status",
            type="dynamic_key_object",
            collection="race_weekends_v2",
            field="sessions.race.results_by_status",
            query_patterns=[
                "status bucket aggregation",
                "classification outcome filtering",
            ],
            required_constructs=["$objectToArray", "$unwind", "$group"],
            provenance_refs=["results", "status", "drivers", "constructors"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "status bucket aggregation",
                        "intent": "summarize finish-status buckets inside each race weekend document",
                        "pipeline": [
                            {
                                "$project": {
                                    "race_id": 1,
                                    "race": "$calendar.race_name",
                                    "status_buckets": {
                                        "$objectToArray": "$sessions.race.results_by_status"
                                    },
                                }
                            },
                            {"$unwind": "$status_buckets"},
                            {
                                "$project": {
                                    "_id": 0,
                                    "race_id": 1,
                                    "race": 1,
                                    "status": "$status_buckets.k",
                                    "driver_count": "$status_buckets.v.count",
                                    "points_total": "$status_buckets.v.points_total",
                                }
                            },
                            {"$match": {"driver_count": {"$gt": 0}}},
                            {"$sort": {"points_total": -1, "race_id": 1, "status": 1}},
                            {"$limit": 25},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind"],
                    },
                    {
                        "query_pattern": "classification outcome filtering",
                        "intent": "pull classified finishers from dynamic status buckets without flattening the original schema",
                        "pipeline": [
                            {
                                "$addFields": {
                                    "status_buckets": {
                                        "$objectToArray": "$sessions.race.results_by_status"
                                    }
                                }
                            },
                            {
                                "$addFields": {
                                    "native_matching_dynamic_keys": {
                                        "$filter": {
                                            "input": "$status_buckets",
                                            "as": "bucket",
                                            "cond": {"$eq": ["$$bucket.k", "Finished"]},
                                        }
                                    }
                                }
                            },
                            {
                                "$match": {
                                    "$expr": {
                                        "$gt": [{"$size": "$native_matching_dynamic_keys"}, 0]
                                    }
                                }
                            },
                            {
                                "$project": {
                                    "_id": 0,
                                    "race_id": 1,
                                    "race": "$calendar.race_name",
                                    "finished": {
                                        "$arrayElemAt": ["$native_matching_dynamic_keys.v", 0]
                                    },
                                }
                            },
                            {"$limit": 25},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$filter", "$size"],
                    },
                ]
            },
        ),
        NativeFeature(
            id="race_weekends_v2.laps_by_number",
            type="dynamic_key_object",
            collection="race_weekends_v2",
            field="sessions.race.laps_by_number",
            query_patterns=[
                "lap running order dynamic object",
                "lap telemetry dynamic object",
            ],
            required_constructs=["$objectToArray", "$unwind", "$sort"],
            provenance_refs=["lapTimes", "results", "drivers", "constructors"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "lap running order dynamic object",
                        "intent": "count lap-leading drivers by traversing the dynamic lap-number object",
                        "pipeline": [
                            {
                                "$project": {
                                    "race_id": 1,
                                    "race": "$calendar.race_name",
                                    "laps": {
                                        "$objectToArray": "$sessions.race.laps_by_number"
                                    },
                                }
                            },
                            {"$unwind": "$laps"},
                            {"$unwind": "$laps.v.running_order"},
                            {"$match": {"laps.v.running_order.position": 1}},
                            {
                                "$group": {
                                    "_id": {
                                        "race_id": "$race_id",
                                        "driver": "$laps.v.running_order.driver.ref",
                                    },
                                    "race": {"$first": "$race"},
                                    "led_laps": {"$sum": 1},
                                }
                            },
                            {"$sort": {"led_laps": -1, "_id.race_id": 1, "_id.driver": 1}},
                            {"$limit": 25},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$group"],
                    },
                    {
                        "query_pattern": "lap telemetry dynamic object",
                        "intent": "extract first-place lap telemetry from dynamic lap-number buckets",
                        "pipeline": [
                            {
                                "$project": {
                                    "race_id": 1,
                                    "race": "$calendar.race_name",
                                    "laps": {
                                        "$objectToArray": "$sessions.race.laps_by_number"
                                    },
                                }
                            },
                            {"$unwind": "$laps"},
                            {
                                "$project": {
                                    "_id": 0,
                                    "race_id": 1,
                                    "race": 1,
                                    "lap": "$laps.k",
                                    "leaders": {
                                        "$filter": {
                                            "input": "$laps.v.running_order",
                                            "as": "entry",
                                            "cond": {"$eq": ["$$entry.position", 1]},
                                        }
                                    },
                                }
                            },
                            {"$match": {"$expr": {"$gt": [{"$size": "$leaders"}, 0]}}},
                            {"$sort": {"race_id": 1, "lap": 1}},
                            {"$limit": 50},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$filter", "$size"],
                    },
                ]
            },
        ),
        NativeFeature(
            id="race_weekends_v2.driver_pace_profiles",
            type="nested_event_stream",
            collection="race_weekends_v2",
            field="sessions.race.entries.pace_profile.laps_by_number",
            query_patterns=[
                "lap telemetry dynamic object",
                "per-driver pace profile",
            ],
            required_constructs=["$map", "$objectToArray", "$filter"],
            provenance_refs=["lapTimes", "results", "pitStops"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "lap telemetry dynamic object",
                        "intent": "keep race entries whose per-driver pace profile contains lap telemetry",
                        "pipeline": [
                            {
                                "$project": {
                                    "race_id": 1,
                                    "race": "$calendar.race_name",
                                    "feature_path": "$sessions.race.entries.pace_profile.laps_by_number",
                                    "entries": "$sessions.race.entries",
                                }
                            },
                            {
                                "$addFields": {
                                    "native_filtered_events": {
                                        "$filter": {
                                            "input": {"$ifNull": ["$entries", []]},
                                            "as": "entry",
                                            "cond": {
                                                "$gt": [
                                                    {
                                                        "$size": {
                                                            "$objectToArray": {
                                                                "$ifNull": [
                                                                    "$$entry.pace_profile.laps_by_number",
                                                                    {},
                                                                ]
                                                            }
                                                        }
                                                    },
                                                    0,
                                                ]
                                            },
                                        }
                                    }
                                }
                            },
                            {
                                "$match": {
                                    "$expr": {
                                        "$gt": [{"$size": "$native_filtered_events"}, 0]
                                    }
                                }
                            },
                            {
                                "$project": {
                                    "_id": 0,
                                    "race_id": 1,
                                    "race": 1,
                                    "native_filtered_events.driver": 1,
                                    "native_filtered_events.pace_profile.laps_by_number": 1,
                                }
                            },
                            {"$limit": 25},
                        ],
                        "mongo_native_constructs": ["$filter", "$objectToArray", "$size", "$ifNull"],
                    },
                    {
                        "query_pattern": "per-driver pace profile",
                        "intent": "map race entries into driver pace summaries with lap-count evidence",
                        "pipeline": [
                            {
                                "$project": {
                                    "race_id": 1,
                                    "race": "$calendar.race_name",
                                    "feature_path": "$sessions.race.entries.pace_profile.laps_by_number",
                                    "native_filtered_events": {
                                        "$filter": {
                                            "input": {"$ifNull": ["$sessions.race.entries", []]},
                                            "as": "entry",
                                            "cond": {
                                                "$gt": [
                                                    {
                                                        "$size": {
                                                            "$objectToArray": {
                                                                "$ifNull": [
                                                                    "$$entry.pace_profile.laps_by_number",
                                                                    {},
                                                                ]
                                                            }
                                                        }
                                                    },
                                                    10,
                                                ]
                                            },
                                        }
                                    },
                                }
                            },
                            {
                                "$match": {
                                    "$expr": {
                                        "$gt": [{"$size": "$native_filtered_events"}, 0]
                                    }
                                }
                            },
                            {"$sort": {"race_id": 1}},
                            {"$limit": 25},
                        ],
                        "mongo_native_constructs": ["$filter", "$objectToArray", "$size", "$ifNull"],
                    },
                ]
            },
        ),
        NativeFeature(
            id="race_weekends_v2.qualifying_windows",
            type="nested_event_stream",
            collection="race_weekends_v2",
            field="sessions.qualifying.elimination_windows",
            query_patterns=["qualifying elimination window", "driver reference lookup"],
            required_constructs=["$unwind", "$sort", "$limit"],
            provenance_refs=["qualifying", "drivers"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "qualifying elimination window",
                        "intent": "filter Q1 elimination-window entries embedded in each race weekend",
                        "pipeline": [
                            {
                                "$project": {
                                    "race_id": 1,
                                    "race": "$calendar.race_name",
                                    "feature_path": "$sessions.qualifying.elimination_windows",
                                    "q1_entries": {
                                        "$ifNull": [
                                            "$sessions.qualifying.elimination_windows.q1_slowest_five.entries",
                                            [],
                                        ]
                                    },
                                }
                            },
                            {
                                "$addFields": {
                                    "native_filtered_events": {
                                        "$filter": {
                                            "input": "$q1_entries",
                                            "as": "entry",
                                            "cond": {"$ne": ["$$entry.driver.ref", None]},
                                        }
                                    }
                                }
                            },
                            {
                                "$match": {
                                    "$expr": {
                                        "$gt": [{"$size": "$native_filtered_events"}, 0]
                                    }
                                }
                            },
                            {"$sort": {"race_id": 1}},
                            {"$limit": 25},
                        ],
                        "mongo_native_constructs": ["$filter", "$ifNull", "$size"],
                    },
                    {
                        "query_pattern": "driver reference lookup",
                        "intent": "return qualifying elimination driver references from the nested window object",
                        "pipeline": [
                            {
                                "$project": {
                                    "_id": 0,
                                    "race_id": 1,
                                    "race": "$calendar.race_name",
                                    "feature_path": "$sessions.qualifying.elimination_windows",
                                    "native_filtered_events": {
                                        "$filter": {
                                            "input": {
                                                "$ifNull": [
                                                    "$sessions.qualifying.elimination_windows.q1_slowest_five.entries",
                                                    [],
                                                ]
                                            },
                                            "as": "entry",
                                            "cond": {"$ne": ["$$entry.driver.ref", None]},
                                        }
                                    },
                                }
                            },
                            {"$match": {"$expr": {"$gt": [{"$size": "$native_filtered_events"}, 0]}}},
                            {"$limit": 25},
                        ],
                        "mongo_native_constructs": ["$filter", "$ifNull", "$size"],
                    },
                ]
            },
        ),
        NativeFeature(
            id="f1_actor_profiles.polymorphic_profiles",
            type="polymorphic_collection",
            collection="f1_actor_profiles",
            field="entity_type",
            query_patterns=[
                "driver constructor circuit profile union",
                "actor career history by year",
            ],
            required_constructs=["$match", "$project"],
            provenance_refs=["drivers", "constructors", "circuits", "results", "races"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "driver constructor circuit profile union",
                        "intent": "dispatch actor profile documents by driver constructor circuit subtype",
                        "pipeline": [
                            {
                                "$addFields": {
                                    "native_subtype_bucket": {
                                        "$switch": {
                                            "branches": [
                                                {
                                                    "case": {"$eq": ["$entity_type", "driver"]},
                                                    "then": "driver",
                                                },
                                                {
                                                    "case": {"$eq": ["$entity_type", "constructor"]},
                                                    "then": "constructor",
                                                },
                                                {
                                                    "case": {"$eq": ["$entity_type", "circuit"]},
                                                    "then": "circuit",
                                                },
                                            ],
                                            "default": "other",
                                        }
                                    }
                                }
                            },
                            {"$match": {"native_subtype_bucket": {"$ne": "other"}}},
                            {
                                "$project": {
                                    "_id": 1,
                                    "entity_type": 1,
                                    "native_subtype_bucket": 1,
                                    "driver": 1,
                                    "constructor": 1,
                                    "circuit": 1,
                                }
                            },
                            {"$sort": {"native_subtype_bucket": 1, "_id": 1}},
                            {"$limit": 50},
                        ],
                        "mongo_native_constructs": ["$switch"],
                    },
                    {
                        "query_pattern": "actor career history by year",
                        "intent": "dispatch actor profiles and expose year-keyed driver career history",
                        "pipeline": [
                            {
                                "$addFields": {
                                    "native_subtype_bucket": {
                                        "$switch": {
                                            "branches": [
                                                {
                                                    "case": {"$eq": ["$entity_type", "driver"]},
                                                    "then": "driver",
                                                },
                                                {
                                                    "case": {"$eq": ["$entity_type", "constructor"]},
                                                    "then": "constructor",
                                                },
                                                {
                                                    "case": {"$eq": ["$entity_type", "circuit"]},
                                                    "then": "circuit",
                                                },
                                            ],
                                            "default": "other",
                                        }
                                    }
                                }
                            },
                            {"$match": {"native_subtype_bucket": "driver"}},
                            {
                                "$project": {
                                    "_id": 1,
                                    "entity_type": 1,
                                    "driver": 1,
                                    "career_years": {"$objectToArray": "$career_by_year"},
                                    "native_subtype_bucket": 1,
                                }
                            },
                            {"$unwind": "$career_years"},
                            {
                                "$project": {
                                    "_id": 1,
                                    "driver.ref": 1,
                                    "year": "$career_years.k",
                                    "race_count": "$career_years.v.race_count",
                                    "points": "$career_years.v.points",
                                    "native_subtype_bucket": 1,
                                }
                            },
                            {"$sort": {"points": -1, "_id": 1, "year": 1}},
                            {"$limit": 50},
                        ],
                        "mongo_native_constructs": ["$switch", "$objectToArray", "$unwind"],
                    }
                ]
            },
        ),
    ]
