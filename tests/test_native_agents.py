from __future__ import annotations

from types import SimpleNamespace

from tend.agents import get_agent


def test_native_agents_register_with_distinct_ids():
    assert type(get_agent("native_migration_designer")).__name__ == "NativeMigrationDesigner"
    assert type(get_agent("native_nl_generator")).__name__ == "NativeNlGenerator"


def test_native_migration_designer_rejects_non_native_recipe():
    agent = get_agent("native_migration_designer")
    output = {
        "db_id": "financial",
        "recipe_version": 1,
        "design_goal": "Only embeds source tables without native shape changes.",
        "collections": {
            "account": {
                "source_tables": ["account"],
                "transforms": [{"id": "plain", "type": "reference_collection"}],
            }
        },
    }

    violations = agent.check_contract(SimpleNamespace(source=None), {}, output)

    assert "recipe lacks MongoDB-native features" in violations
    assert "recipe only describes simple embedding/reference structure" in violations


def test_native_nl_generator_requires_exact_nl_keys():
    agent = get_agent("native_nl_generator")

    violations = agent.check_contract(
        SimpleNamespace(),
        {},
        {
            "nl_queries": {
                "canonical": "Find matching native records.",
                "colloquial": "Show the matching native records.",
                "extra": "not allowed",
            }
        },
    )

    assert violations == ["nl_queries must contain exactly canonical and colloquial"]
