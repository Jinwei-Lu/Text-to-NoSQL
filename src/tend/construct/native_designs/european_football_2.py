from __future__ import annotations

from collections import defaultdict
from typing import Any

from ...execution import world_signature as compute_world_signature
from ..native_executor import NativeExecutionResult
from ..native_recipe import NativeFeature, NativeFeatureManifest, NativeMigrationRecipe
from .common import collection, expr, join, recipe, source as field_source, transform

DESIGN_VERSION = 1
MODULE_REF = __name__


def build_native_recipe(source: Any, db_id: str) -> NativeMigrationRecipe:
    source.schema(db_id)
    return recipe(
        db_id,
        version=DESIGN_VERSION,
        design_goal=(
            "Represent European football teams with season-keyed match summaries, "
            "team-attribute timelines, and typed league/player/team entities."
        ),
        collections=[
            collection(
                "football_team_profiles",
                purpose="Team documents with season-keyed home-match performance.",
                source_tables=["Team", "Match", "Team_Attributes"],
                transforms=[
                    transform(
                        "home_matches_by_season",
                        "dynamic_key_object",
                        module_ref=MODULE_REF,
                        parent_table="Team",
                        child_table="Match",
                        join=join("Team.team_api_id", "Match.home_team_api_id"),
                        target_field="home_matches_by_season",
                        key=expr("Match.season", "Match.season"),
                        values={
                            "home_goals": expr("sum(Match.home_team_goal)", "Match.home_team_goal"),
                            "away_goals_allowed": expr(
                                "sum(Match.away_team_goal)",
                                "Match.away_team_goal",
                            ),
                            "matches": expr("count(Match.id)", "Match.id"),
                        },
                    ),
                    transform(
                        "team_attribute_events",
                        "nested_event_stream",
                        module_ref=MODULE_REF,
                        parent_table="Team",
                        event_source_table="Team_Attributes",
                        join=join("Team.team_api_id", "Team_Attributes.team_api_id"),
                        target_field="attribute_history",
                        event_type_field="Team_Attributes.buildUpPlaySpeedClass",
                        event_time_field="Team_Attributes.date",
                        event_payload={
                            "build_up_speed": "Team_Attributes.buildUpPlaySpeed",
                            "chance_passing": "Team_Attributes.chanceCreationPassing",
                            "defence_pressure": "Team_Attributes.defencePressure",
                            "defender_line": "Team_Attributes.defenceDefenderLineClass",
                        },
                    ),
                ],
            ),
            collection(
                "football_entities",
                purpose="Typed league, player, and team entities.",
                source_tables=["League", "Player", "Team", "Country"],
                transforms=[
                    transform(
                        "football_entity_union",
                        "polymorphic_union",
                        module_ref=MODULE_REF,
                        discriminator="entity_type",
                        variants={
                            "league": {
                                "source_table": "League",
                                "fields": {
                                    "entity_id": expr("concat('league:', League.id)", "League.id"),
                                    "name": field_source("League.name"),
                                    "country_id": field_source("League.country_id"),
                                },
                            },
                            "player": {
                                "source_table": "Player",
                                "fields": {
                                    "entity_id": expr(
                                        "concat('player:', Player.player_api_id)",
                                        "Player.player_api_id",
                                    ),
                                    "name": field_source("Player.player_name"),
                                    "birthday": field_source("Player.birthday"),
                                },
                            },
                            "team": {
                                "source_table": "Team",
                                "fields": {
                                    "entity_id": expr(
                                        "concat('team:', Team.team_api_id)",
                                        "Team.team_api_id",
                                    ),
                                    "long_name": field_source("Team.team_long_name"),
                                    "short_name": field_source("Team.team_short_name"),
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
    if db_id != "european_football_2":
        raise ValueError(f"european_football_2 materializer received db_id={db_id!r}")
    schema = source.schema(db_id)
    conn = source.connection(db_id)

    countries = _by_id(_rows(conn, "Country", ["id"]), "id")
    leagues = _by_id(_rows(conn, "League", ["id"]), "id")
    teams = _by_id(_rows(conn, "Team", ["team_api_id"]), "team_api_id")
    players = _by_id(_rows(conn, "Player", ["player_api_id"]), "player_api_id")
    matches = _rows(conn, "Match", ["season", "date", "id"])
    team_attributes = _group(_rows(conn, "Team_Attributes", ["team_api_id", "date", "id"]), "team_api_id")
    player_attributes = _group(_rows(conn, "Player_Attributes", ["player_api_id", "date", "id"]), "player_api_id")

    player_rating_by_year = {
        player_id: _player_rating_by_year(rows)
        for player_id, rows in player_attributes.items()
    }
    team_match_index = _team_match_index(matches)

    match_docs = [
        _match_document(
            match,
            countries=countries,
            leagues=leagues,
            teams=teams,
            players=players,
            player_rating_by_year=player_rating_by_year,
        )
        for match in matches
    ]
    team_docs = [
        _team_profile(
            team,
            matches=team_match_index.get(team_id, []),
            attributes=team_attributes.get(team_id, []),
            countries=countries,
            leagues=leagues,
            teams=teams,
            players=players,
            player_rating_by_year=player_rating_by_year,
        )
        for team_id, team in sorted(teams.items())
    ]
    player_docs = [
        _player_profile(player, player_attributes.get(player_id, []))
        for player_id, player in sorted(players.items())
    ]
    league_docs = _league_season_buckets(matches, countries, leagues, teams)

    data = {
        "league_season_buckets": league_docs,
        "match_documents": match_docs,
        "player_profiles": player_docs,
        "team_profiles": team_docs,
    }
    manifest = NativeFeatureManifest(db_id=db_id, features=_native_features())
    native_schema = {
        "db_id": db_id,
        "source_tables": list(schema.tables),
        "collections": {
            "match_documents": {
                "document_count": len(match_docs),
                "root_entity": "match",
                "source_tables": ["Match", "Team", "Player", "League", "Country"],
            },
            "team_profiles": {
                "document_count": len(team_docs),
                "root_entity": "team",
                "source_tables": ["Team", "Match", "Team_Attributes", "Player"],
            },
            "player_profiles": {
                "document_count": len(player_docs),
                "root_entity": "player",
                "source_tables": ["Player", "Player_Attributes"],
            },
            "league_season_buckets": {
                "document_count": len(league_docs),
                "root_entity": "league season",
                "source_tables": ["League", "Country", "Match", "Team"],
            },
        },
    }
    provenance = {
        feature.id: {
            "module": MODULE_REF,
            "source_tables": _source_tables(feature.provenance_refs),
            "field": feature.field,
        }
        for feature in manifest.features
    }
    signature = compute_world_signature(data)
    if event_hook is not None:
        event_hook(
            "european_football_native_materialized",
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


def _by_id(rows: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    return {row.get(key): row for row in rows if row.get(key) is not None}


def _group(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    out: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[row.get(key)].append(row)
    return dict(out)


def _team_match_index(matches: list[dict[str, Any]]) -> dict[Any, list[dict[str, Any]]]:
    out: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for match in matches:
        out[match.get("home_team_api_id")].append(match)
        out[match.get("away_team_api_id")].append(match)
    return {team_id: rows for team_id, rows in out.items() if team_id is not None}


def _match_document(
    match: dict[str, Any],
    *,
    countries: dict[Any, dict[str, Any]],
    leagues: dict[Any, dict[str, Any]],
    teams: dict[Any, dict[str, Any]],
    players: dict[Any, dict[str, Any]],
    player_rating_by_year: dict[Any, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    league = leagues.get(match.get("league_id"), {})
    country = countries.get(match.get("country_id") or league.get("country_id"), {})
    home_team = teams.get(match.get("home_team_api_id"), {})
    away_team = teams.get(match.get("away_team_api_id"), {})
    home_players = _lineup_players(match, "home", players, player_rating_by_year)
    away_players = _lineup_players(match, "away", players, player_rating_by_year)
    return {
        "_id": f"match:{match.get('id')}",
        "identity": {
            "match_id": match.get("id"),
            "match_api_id": match.get("match_api_id"),
            "match_api_id_state": _presence(match.get("match_api_id")),
        },
        "competition": {
            "country": {"id": country.get("id"), "name": country.get("name")},
            "league": {"id": league.get("id"), "name": league.get("name")},
            "season": match.get("season"),
            "stage": match.get("stage"),
            "date": {"value": match.get("date"), "state": _presence(match.get("date"))},
        },
        "teams": {
            "home": _team_snapshot(home_team),
            "away": _team_snapshot(away_team),
        },
        "scoreline": {
            "home_goals": match.get("home_team_goal"),
            "away_goals": match.get("away_team_goal"),
            "status_bucket": _result_bucket(match),
            "goal_total": (match.get("home_team_goal") or 0) + (match.get("away_team_goal") or 0),
        },
        "lineups": {
            "home": {
                "players": home_players,
                "formation_slots_by_role": _formation_slots(home_players),
                "schema_state": _presence(home_players),
            },
            "away": {
                "players": away_players,
                "formation_slots_by_role": _formation_slots(away_players),
                "schema_state": _presence(away_players),
            },
        },
        "betting_market": {
            "bookmakers_by_code": _bookmaker_odds(match),
            "schema_state": _presence(_bookmaker_odds(match)),
        },
        "observability": {
            "goal_event_feed_state": _presence(match.get("goal")),
            "shot_feed_state": _presence(match.get("shoton")),
            "card_feed_state": _presence(match.get("card")),
            "possession_feed_state": _presence(match.get("possession")),
            "lineup_coordinates_state": _presence(
                [match.get("home_player_X1"), match.get("away_player_X1")]
            ),
        },
    }


def _team_profile(
    team: dict[str, Any],
    *,
    matches: list[dict[str, Any]],
    attributes: list[dict[str, Any]],
    countries: dict[Any, dict[str, Any]],
    leagues: dict[Any, dict[str, Any]],
    teams: dict[Any, dict[str, Any]],
    players: dict[Any, dict[str, Any]],
    player_rating_by_year: dict[Any, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    players_seen = _team_players(matches, team.get("team_api_id"), players, player_rating_by_year)
    return {
        "_id": f"team:{team.get('team_api_id')}",
        "team": _team_snapshot(team),
        "matches_by_season": _matches_by_season(matches, team, countries, leagues, teams),
        "attribute_timeline": {
            "by_year": _team_attributes_by_year(attributes),
            "schema_state": _presence(attributes),
        },
        "players": players_seen[:40],
        "schema_state": {
            "matches": _presence(matches),
            "attributes": _presence(attributes),
            "players": _presence(players_seen),
            "external_injury_feed": "missing",
        },
    }


def _player_profile(
    player: dict[str, Any],
    attributes: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "_id": f"player:{player.get('player_api_id')}",
        "player": _player_snapshot(player),
        "attribute_timeline": {
            "by_year": _player_attributes_by_year(attributes),
            "preferred_foot_state": _presence(
                next((row.get("preferred_foot") for row in attributes if row.get("preferred_foot")), None)
            ),
        },
        "rating_by_season": _player_rating_by_year(attributes),
        "schema_state": {
            "profile": _presence(player),
            "attributes": _presence(attributes),
            "birthday": _presence(player.get("birthday")),
            "external_transfer_feed": "missing",
        },
    }


def _league_season_buckets(
    matches: list[dict[str, Any]],
    countries: dict[Any, dict[str, Any]],
    leagues: dict[Any, dict[str, Any]],
    teams: dict[Any, dict[str, Any]],
) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, str], list[dict[str, Any]]] = defaultdict(list)
    for match in matches:
        buckets[(match.get("league_id"), str(match.get("season")))].append(match)
    docs: list[dict[str, Any]] = []
    for (league_id, season), rows in sorted(buckets.items(), key=lambda item: (str(item[0][0]), item[0][1])):
        league = leagues.get(league_id, {})
        country = countries.get(league.get("country_id"), {})
        docs.append(
            {
                "_id": f"league:{league_id}:season:{season}",
                "league": {"id": league_id, "name": league.get("name")},
                "country": {"id": country.get("id"), "name": country.get("name")},
                "season": season,
                "teams_by_result_bucket": _teams_by_result_bucket(rows, teams),
                "team_table": _team_table(rows, teams),
                "matches_by_stage": _matches_by_stage(rows, teams),
                "schema_state": {
                    "matches": _presence(rows),
                    "country": _presence(country),
                    "external_standings_feed": "missing",
                },
            }
        )
    return docs


def _lineup_players(
    match: dict[str, Any],
    side: str,
    players: dict[Any, dict[str, Any]],
    player_rating_by_year: dict[Any, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for slot in range(1, 12):
        player_id = match.get(f"{side}_player_{slot}")
        if player_id is None:
            continue
        x_value = match.get(f"{side}_player_X{slot}")
        y_value = match.get(f"{side}_player_Y{slot}")
        out.append(
            {
                "slot": slot,
                "role": _role_from_coordinates(x_value, y_value),
                "coordinates": {
                    "x": x_value,
                    "y": y_value,
                    "state": _presence([x_value, y_value]),
                },
                "player": _player_snapshot(players.get(player_id, {"player_api_id": player_id})),
                "rating_by_season": player_rating_by_year.get(player_id, {}),
            }
        )
    return out


def _formation_slots(players: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for player in players:
        role = _dynamic_key(player.get("role"), "unknown")
        out.setdefault(role, {"players": []})
        out[role]["players"].append(
            {
                "slot": player.get("slot"),
                "player": player.get("player"),
                "coordinates": player.get("coordinates"),
            }
        )
    return dict(sorted(out.items()))


def _team_players(
    matches: list[dict[str, Any]],
    team_id: Any,
    players: dict[Any, dict[str, Any]],
    player_rating_by_year: dict[Any, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    seen: dict[Any, dict[str, Any]] = {}
    for match in matches:
        side = "home" if match.get("home_team_api_id") == team_id else "away"
        for player in _lineup_players(match, side, players, player_rating_by_year):
            player_id = player.get("player", {}).get("player_api_id")
            if player_id is not None and player_id not in seen:
                seen[player_id] = {
                    "player": player.get("player"),
                    "first_seen_match_id": match.get("id"),
                    "rating_by_season": player.get("rating_by_season", {}),
                }
    return list(seen.values())


def _matches_by_season(
    matches: list[dict[str, Any]],
    team: dict[str, Any],
    countries: dict[Any, dict[str, Any]],
    leagues: dict[Any, dict[str, Any]],
    teams: dict[Any, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    team_id = team.get("team_api_id")
    out: dict[str, dict[str, Any]] = {}
    for match in matches:
        season = _dynamic_key(match.get("season"), "unknown")
        bucket = out.setdefault(
            season,
            {
                "summary": {"played": 0, "goals_for": 0, "goals_against": 0, "wins": 0, "draws": 0},
                "fixtures": [],
            },
        )
        side = "home" if match.get("home_team_api_id") == team_id else "away"
        home_goals = int(match.get("home_team_goal") or 0)
        away_goals = int(match.get("away_team_goal") or 0)
        goals_for = home_goals if side == "home" else away_goals
        goals_against = away_goals if side == "home" else home_goals
        bucket["summary"]["played"] += 1
        bucket["summary"]["goals_for"] += goals_for
        bucket["summary"]["goals_against"] += goals_against
        if goals_for > goals_against:
            bucket["summary"]["wins"] += 1
        if goals_for == goals_against:
            bucket["summary"]["draws"] += 1
        if len(bucket["fixtures"]) < 80:
            league = leagues.get(match.get("league_id"), {})
            country = countries.get(match.get("country_id") or league.get("country_id"), {})
            opponent_id = match.get("away_team_api_id") if side == "home" else match.get("home_team_api_id")
            bucket["fixtures"].append(
                {
                    "match_id": match.get("id"),
                    "date": match.get("date"),
                    "stage": match.get("stage"),
                    "side": side,
                    "country": country.get("name"),
                    "league": league.get("name"),
                    "opponent": _team_snapshot(teams.get(opponent_id, {})),
                    "score": {"for": goals_for, "against": goals_against},
                    "result_bucket": _result_bucket_for_team(goals_for, goals_against),
                }
            )
    return dict(sorted(out.items()))


def _team_attributes_by_year(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        year = _year(row.get("date"))
        out.setdefault(year, {"snapshots": []})
        out[year]["snapshots"].append(
            {
                "date": row.get("date"),
                "build_up": {
                    "speed": row.get("buildUpPlaySpeed"),
                    "speed_class": row.get("buildUpPlaySpeedClass"),
                    "passing": row.get("buildUpPlayPassing"),
                    "passing_class": row.get("buildUpPlayPassingClass"),
                },
                "chance_creation": {
                    "passing": row.get("chanceCreationPassing"),
                    "crossing": row.get("chanceCreationCrossing"),
                    "shooting": row.get("chanceCreationShooting"),
                },
                "defence": {
                    "pressure": row.get("defencePressure"),
                    "aggression": row.get("defenceAggression"),
                    "width": row.get("defenceTeamWidth"),
                    "line": row.get("defenceDefenderLineClass"),
                },
                "presence_state": _presence(row),
            }
        )
    return dict(sorted(out.items()))


def _player_attributes_by_year(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        year = _year(row.get("date"))
        out.setdefault(year, {"snapshots": []})
        if len(out[year]["snapshots"]) >= 8:
            continue
        out[year]["snapshots"].append(
            {
                "date": row.get("date"),
                "overall_rating": row.get("overall_rating"),
                "potential": row.get("potential"),
                "pace": {
                    "acceleration": row.get("acceleration"),
                    "sprint_speed": row.get("sprint_speed"),
                    "agility": row.get("agility"),
                },
                "technical": {
                    "ball_control": row.get("ball_control"),
                    "dribbling": row.get("dribbling"),
                    "short_passing": row.get("short_passing"),
                    "finishing": row.get("finishing"),
                },
                "work_rate": {
                    "attacking": row.get("attacking_work_rate"),
                    "defensive": row.get("defensive_work_rate"),
                },
            }
        )
    return dict(sorted(out.items()))


def _player_rating_by_year(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        year = _year(row.get("date"))
        current = out.get(year)
        if current is None or str(row.get("date") or "") > str(current.get("latest_date") or ""):
            out[year] = {
                "latest_date": row.get("date"),
                "overall_rating": row.get("overall_rating"),
                "potential": row.get("potential"),
                "sprint_speed": row.get("sprint_speed"),
                "state": _presence(row.get("overall_rating")),
            }
    return dict(sorted(out.items()))


def _teams_by_result_bucket(
    matches: list[dict[str, Any]],
    teams: dict[Any, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {
        "home_win": {"teams": []},
        "away_win": {"teams": []},
        "draw": {"teams": []},
        "scheduled_or_missing_score": {"teams": []},
    }
    for match in matches:
        bucket = _result_bucket(match)
        home_goals = int(match.get("home_team_goal") or 0)
        away_goals = int(match.get("away_team_goal") or 0)
        team_id = match.get("home_team_api_id") if bucket != "away_win" else match.get("away_team_api_id")
        out.setdefault(bucket, {"teams": []})
        if len(out[bucket]["teams"]) < 120:
            out[bucket]["teams"].append(
                {
                    "match_id": match.get("id"),
                    "team": _team_snapshot(teams.get(team_id, {})),
                    "score": {"home": home_goals, "away": away_goals},
                }
            )
    return out


def _team_table(
    matches: list[dict[str, Any]],
    teams: dict[Any, dict[str, Any]],
) -> list[dict[str, Any]]:
    table: dict[Any, dict[str, Any]] = {}
    for match in matches:
        for side in ("home", "away"):
            team_id = match.get(f"{side}_team_api_id")
            if team_id is None:
                continue
            row = table.setdefault(
                team_id,
                {
                    "team": _team_snapshot(teams.get(team_id, {})),
                    "played": 0,
                    "goals_for": 0,
                    "goals_against": 0,
                    "result_buckets_by_result": {
                        "wins": {"matches": []},
                        "draws": {"matches": []},
                        "losses": {"matches": []},
                    },
                },
            )
            home_goals = int(match.get("home_team_goal") or 0)
            away_goals = int(match.get("away_team_goal") or 0)
            goals_for = home_goals if side == "home" else away_goals
            goals_against = away_goals if side == "home" else home_goals
            result = _result_bucket_for_team(goals_for, goals_against)
            key = {"win": "wins", "draw": "draws", "loss": "losses"}[result]
            row["played"] += 1
            row["goals_for"] += goals_for
            row["goals_against"] += goals_against
            if len(row["result_buckets_by_result"][key]["matches"]) < 40:
                row["result_buckets_by_result"][key]["matches"].append(
                    {"match_id": match.get("id"), "side": side, "score": {"for": goals_for, "against": goals_against}}
                )
    return sorted(table.values(), key=lambda row: (-int(row["played"]), str(row["team"].get("long_name"))))


def _matches_by_stage(
    matches: list[dict[str, Any]],
    teams: dict[Any, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for match in matches:
        stage = str(match.get("stage") or "unknown")
        bucket = out.setdefault(stage, {"fixtures": []})
        if len(bucket["fixtures"]) < 80:
            bucket["fixtures"].append(
                {
                    "match_id": match.get("id"),
                    "date": match.get("date"),
                    "home": _team_snapshot(teams.get(match.get("home_team_api_id"), {})),
                    "away": _team_snapshot(teams.get(match.get("away_team_api_id"), {})),
                    "result_bucket": _result_bucket(match),
                }
            )
    return dict(sorted(out.items(), key=lambda item: item[0]))


def _team_snapshot(team: dict[str, Any]) -> dict[str, Any]:
    return {
        "team_api_id": team.get("team_api_id"),
        "team_fifa_api_id": team.get("team_fifa_api_id"),
        "long_name": team.get("team_long_name"),
        "short_name": team.get("team_short_name"),
        "name_state": _presence(team.get("team_long_name")),
    }


def _player_snapshot(player: dict[str, Any]) -> dict[str, Any]:
    return {
        "player_api_id": player.get("player_api_id"),
        "player_fifa_api_id": player.get("player_fifa_api_id"),
        "name": player.get("player_name"),
        "birthday": {"value": player.get("birthday"), "state": _presence(player.get("birthday"))},
        "body": {
            "height": {"value": player.get("height"), "state": _presence(player.get("height"))},
            "weight": {"value": player.get("weight"), "state": _presence(player.get("weight"))},
        },
    }


def _bookmaker_odds(match: dict[str, Any]) -> dict[str, dict[str, Any]]:
    codes = ["B365", "BW", "IW", "LB", "PS", "WH", "SJ", "VC", "GB", "BS"]
    out: dict[str, dict[str, Any]] = {}
    for code in codes:
        home = match.get(f"{code}H")
        draw = match.get(f"{code}D")
        away = match.get(f"{code}A")
        if home is None and draw is None and away is None:
            continue
        out[code] = {
            "home": home,
            "draw": draw,
            "away": away,
            "state": "present",
        }
    return out


def _role_from_coordinates(x_value: Any, y_value: Any) -> str:
    if y_value is None:
        return "unknown"
    try:
        y = int(y_value)
    except (TypeError, ValueError):
        return "unknown"
    if y <= 2:
        return "goalkeeper_defence"
    if y <= 5:
        return "midfield"
    return "attack"


def _result_bucket(match: dict[str, Any]) -> str:
    home = match.get("home_team_goal")
    away = match.get("away_team_goal")
    if home is None or away is None:
        return "scheduled_or_missing_score"
    if home > away:
        return "home_win"
    if away > home:
        return "away_win"
    return "draw"


def _result_bucket_for_team(goals_for: int, goals_against: int) -> str:
    if goals_for > goals_against:
        return "win"
    if goals_for < goals_against:
        return "loss"
    return "draw"


def _presence(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return "empty" if value == "" else "present"
    if isinstance(value, (list, tuple, dict, set)):
        return "present" if len(value) > 0 else "empty"
    return "present"


def _dynamic_key(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def _year(value: Any) -> str:
    text = str(value or "")
    return text[:4] if len(text) >= 4 else "unknown"


def _source_tables(refs: list[str]) -> list[str]:
    return sorted({ref.split(".", 1)[0] for ref in refs if "." in ref})


def _native_features() -> list[NativeFeature]:
    return [
        NativeFeature(
            id="team_profiles.matches_by_season",
            type="dynamic_key_object",
            collection="team_profiles",
            field="matches_by_season",
            query_patterns=["football_team_season_fixture_matrix"],
            required_constructs=["$objectToArray", "$unwind", "$group", "$sum"],
            provenance_refs=["Team.team_api_id", "Match.season", "Match.home_team_goal", "Match.away_team_goal"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "football_team_season_fixture_matrix",
                        "intent": "summarize team profiles through season-keyed fixture buckets",
                        "pipeline": [
                            {"$project": {"team": "$team.long_name", "feature_path": "$matches_by_season", "seasons": {"$objectToArray": "$matches_by_season"}}},
                            {"$unwind": "$seasons"},
                            {"$unwind": "$seasons.v.fixtures"},
                            {"$group": {"_id": {"team": "$team", "season": "$seasons.k"}, "fixture_count": {"$sum": 1}, "goals_for": {"$sum": "$seasons.v.fixtures.score.for"}}},
                            {"$sort": {"fixture_count": -1, "_id.team": 1}},
                            {"$limit": 50},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$sum"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="match_documents.lineup_rating_context",
            type="nested_event_stream",
            collection="match_documents",
            field="lineups.home.players",
            query_patterns=["football_lineup_rating_context"],
            required_constructs=["$filter", "$objectToArray", "$size"],
            provenance_refs=["Match.home_player_1", "Player.player_api_id", "Player_Attributes.overall_rating"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "football_lineup_rating_context",
                        "intent": "filter match lineups to players with rating-by-season context",
                        "pipeline": [
                            {"$project": {"match_api_id": "$identity.match_api_id", "feature_path": "$lineups.home.players", "home_players": "$lineups.home.players", "away_players": "$lineups.away.players"}},
                            {"$addFields": {"native_filtered_events": {"$filter": {"input": {"$ifNull": ["$home_players", []]}, "as": "player", "cond": {"$gt": [{"$size": {"$objectToArray": {"$ifNull": ["$$player.rating_by_season", {}]}}}, 0]}}}}},
                            {"$match": {"$expr": {"$gt": [{"$size": "$native_filtered_events"}, 0]}}},
                            {"$project": {"_id": 0, "match_api_id": 1, "native_filtered_events.player": 1, "native_filtered_events.rating_by_season": 1}},
                            {"$limit": 50},
                        ],
                        "mongo_native_constructs": ["$filter", "$objectToArray", "$size", "$ifNull"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="player_profiles.attribute_timeline",
            type="dynamic_key_object",
            collection="player_profiles",
            field="attribute_timeline.by_year",
            query_patterns=["football_player_attribute_timeline"],
            required_constructs=["$objectToArray", "$unwind", "$avg"],
            provenance_refs=["Player.player_api_id", "Player_Attributes.date", "Player_Attributes.overall_rating"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "football_player_attribute_timeline",
                        "intent": "summarize player attribute snapshots from year-keyed timelines",
                        "pipeline": [
                            {"$project": {"player": "$player.name", "feature_path": "$attribute_timeline.by_year", "years": {"$objectToArray": "$attribute_timeline.by_year"}}},
                            {"$unwind": "$years"},
                            {"$unwind": "$years.v.snapshots"},
                            {"$group": {"_id": "$years.k", "avg_overall": {"$avg": "$years.v.snapshots.overall_rating"}, "snapshot_count": {"$sum": 1}}},
                            {"$sort": {"_id": 1}},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$avg"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="league_season_buckets.team_table",
            type="dynamic_key_object",
            collection="league_season_buckets",
            field="teams_by_result_bucket",
            query_patterns=["football_league_result_bucket_table"],
            required_constructs=["$objectToArray", "$unwind", "$group", "$size"],
            provenance_refs=["League.id", "Match.season", "Match.home_team_goal", "Match.away_team_goal"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "football_league_result_bucket_table",
                        "intent": "traverse league-season result buckets and count teams in each outcome",
                        "pipeline": [
                            {"$project": {"league": "$league.name", "season": 1, "feature_path": "$teams_by_result_bucket", "buckets": {"$objectToArray": "$teams_by_result_bucket"}}},
                            {"$unwind": "$buckets"},
                            {"$project": {"league": 1, "season": 1, "result_bucket": "$buckets.k", "team_count": {"$size": "$buckets.v.teams"}}},
                            {"$match": {"team_count": {"$gt": 0}}},
                            {"$sort": {"team_count": -1, "league": 1}},
                            {"$limit": 50},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$size"],
                    }
                ]
            },
        ),
    ]
