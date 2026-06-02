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
